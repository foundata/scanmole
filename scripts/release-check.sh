#!/usr/bin/env bash
#
# Local, provider-independent release check for scanmole.
#
# Runs the full quality gate (format, lint, strict type check, tests) on every
# supported Python version, then builds the wheel and source distribution,
# installs the wheel into a clean throwaway environment and runs import and
# command-line smoke tests against the installed artifact, including the
# scanmole-gui launcher's defined behavior without PyGObject.
#
# This is intended to be run before tagging a release. It does not depend on
# any CI service; CI (if added) should call the same steps.
#
# Note: integration tests skip themselves without img2pdf and without the SANE
# "test" backend. For full coverage run
# this on a machine with both available; the per-device smoke checklist is a
# separate, manual step.
#
# Usage:
#   scripts/release-check.sh [PYTHON_VERSION ...]
#   scripts/release-check.sh --artifacts
#
# Without arguments the supported version matrix below is used.
#
# --artifacts validates the artifacts currently in dist/ without rebuilding
# them: the exact files "uv publish" would upload. The main gate runs before
# the version bump and the PyPI README preparation, so it never sees those;
# this mode checks litter, version and tag agreement, the lockstep policy,
# the prepared READMEs and that the working tree differs from HEAD in
# nothing but the prepared READMEs. Run it directly before uploading.

set -euo pipefail

# Temp environments live under $TMPDIR, often on a different filesystem than
# the uv cache; copy instead of hardlink to avoid a noisy fallback warning.
export UV_LINK_MODE=copy

# Supported Python versions (keep in sync with pyproject and the README).
# Override by passing versions as arguments.
SUPPORTED_PYTHONS=("3.12" "3.13" "3.14")
ARTIFACTS_ONLY="no"
if [ "${1:-}" = "--artifacts" ]; then
    ARTIFACTS_ONLY="yes"
elif [ "$#" -gt 0 ]; then
    SUPPORTED_PYTHONS=("$@")
fi

# Resolve the package directory (this script lives in <pkg>/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PKG_DIR"

# Expected distribution and import names.
DIST_NAME="scanmole"
IMPORT_NAME="scanmole"
COMMAND_NAME="scanmole"
GUI_COMMAND_NAME="scanmole-gui"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"; git -C "$PKG_DIR" worktree prune >/dev/null 2>&1 || true' EXIT

log() { printf '\n=== %s ===\n' "$*"; }

require_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        echo "error: 'uv' is required but not found in PATH" >&2
        exit 1
    fi
}

ensure_pythons() {
    # Make sure every supported interpreter is available so the matrix can
    # actually run. `uv python install` is idempotent and a no-op when the
    # version is already present.
    log "Ensure Python interpreters: ${SUPPORTED_PYTHONS[*]}"
    uv python install "${SUPPORTED_PYTHONS[@]}"
}

run_static_checks() {
    # Formatter, linter and type checker are version-independent here
    # (mypy targets the project minimum via pyproject), so run them once.
    log "Static checks (format, lint, type check)"
    uv run ruff format --check packages tests
    uv run ruff check packages tests
    uv run mypy packages/scanmole/src packages/scanmole-gui/src tests
}

run_tests_matrix() {
    for py in "${SUPPORTED_PYTHONS[@]}"; do
        log "Tests on Python ${py}"
        uv run --python "$py" --isolated pytest -q
    done
}

build_artifacts() {
    # Build from a pristine checkout of HEAD: the developer tree carries
    # ignored litter (tool caches, editor droppings) that must never decide
    # what ships. Local uncommitted changes are deliberately not built; a
    # release is a commit, not a working tree.
    log "Build wheels and source distributions (clean checkout of HEAD)"
    if [ -n "$(git status --porcelain)" ]; then
        echo "note: local changes present; artifacts are built from HEAD without them"
    fi
    local clean_dir="${WORK_DIR}/clean-src"
    git worktree add --detach --quiet "$clean_dir" HEAD
    rm -rf dist
    (cd "$clean_dir" && uv build --all-packages --out-dir "${PKG_DIR}/dist")
    git worktree remove --force "$clean_dir"
    ls -1 dist
    check_artifact_hygiene
    check_lockstep_bound dist/*
}

check_lockstep_bound() {
    # Releases are lockstep and the GUI/CLI handshake is directional (a newer
    # GUI refuses an older engine), so scanmole-gui's dependency lower bound
    # must equal its own version, in the sources and in every built GUI
    # artifact handed in as an argument.
    log "Lockstep dependency bound (scanmole-gui needs scanmole>=<own version>)"
    uv run python - "$@" <<'PY'
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

errors: list[str] = []
BOUND = re.compile(r"^scanmole\s*>=\s*([0-9.]+)\s*,\s*<\s*([0-9]+)$")

def check(where: str, version: str, requirements: list[str]) -> None:
    lines = [r for r in requirements if re.match(r"^scanmole\W", r)]
    if len(lines) != 1:
        errors.append(f"{where}: expected exactly one scanmole requirement, got {lines}")
        return
    match = BOUND.match(lines[0].strip())
    if match is None:
        errors.append(f"{where}: requirement {lines[0]!r} is not 'scanmole>=X.Y.Z,<N'")
        return
    lower, upper = match.group(1), match.group(2)
    major = version.split(".")[0]
    if lower != version:
        errors.append(
            f"{where}: lower bound {lower} != scanmole-gui version {version}; "
            "lockstep releases must raise the bound with every release"
        )
    if upper != str(int(major) + 1):
        errors.append(f"{where}: upper bound <{upper} does not cap the next major")

pyproject = tomllib.loads(Path("packages/scanmole-gui/pyproject.toml").read_text())
check(
    "packages/scanmole-gui/pyproject.toml",
    pyproject["project"]["version"],
    pyproject["project"]["dependencies"],
)

def metadata_text(artifact: str) -> str:
    if artifact.endswith(".whl"):
        with zipfile.ZipFile(artifact) as bundle:
            meta = next(n for n in bundle.namelist() if n.endswith(".dist-info/METADATA"))
            return bundle.read(meta).decode("utf-8", errors="replace")
    with tarfile.open(artifact) as bundle:
        meta = next(n for n in bundle.getnames() if n.endswith("/PKG-INFO"))
        member = bundle.extractfile(meta)
        assert member is not None
        return member.read().decode("utf-8", errors="replace")

for artifact in sys.argv[1:]:
    name = Path(artifact).name
    if not (name.startswith("scanmole_gui-") or name.startswith("scanmole-gui-")):
        continue
    version, requirements = "", []
    for line in metadata_text(artifact).splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("Requires-Dist:"):
            requirements.append(line.split(":", 1)[1].strip())
    check(artifact, version, requirements)

if errors:
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    raise SystemExit(1)
print(f"lockstep bound ok ({len(sys.argv) - 1} artifact(s) checked)")
PY
}

check_artifact_hygiene() {
    log "Artifact hygiene (no caches or bytecode inside)"
    uv run python - dist/* <<'PY'
import sys
import tarfile
import zipfile

bad: list[str] = []
for name in sys.argv[1:]:
    if name.endswith(".whl"):
        entries = zipfile.ZipFile(name).namelist()
    else:
        with tarfile.open(name) as archive:
            entries = archive.getnames()
    for entry in entries:
        parts = entry.split("/")
        if any(
            part == "__pycache__" or (part.startswith(".") and "cache" in part)
            for part in parts
        ) or entry.endswith(".pyc"):
            bad.append(f"{name}: {entry}")
if bad:
    print("error: developer litter inside release artifacts:", file=sys.stderr)
    for line in bad:
        print(f"  {line}", file=sys.stderr)
    raise SystemExit(1)
print(f"clean: {len(sys.argv) - 1} artifact(s) checked")
PY
}

validate_artifacts() {
    # Pre-publish validation of dist/ exactly as it lies there. The rebuild
    # after the version bump and the README preparation happens from the
    # working tree on purpose (the prepared READMEs only exist there), so
    # these are the only checks the uploaded bytes ever get.
    if ! ls dist/*.whl >/dev/null 2>&1; then
        echo "error: no artifacts in dist/ -- run 'uv build --all-packages' first" >&2
        exit 1
    fi
    check_artifact_hygiene
    check_lockstep_bound dist/*

    log "Tree state, versions, tag, lockstep and prepared READMEs"
    uv run python - dist/* <<'PY'
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

errors: list[str] = []

# The working tree may differ from the tagged HEAD in exactly the three
# prepared README files; anything else would ship untested content.
allowed = {
    "README.md",
    "packages/scanmole/README.md",
    "packages/scanmole-gui/README.md",
}
status = subprocess.run(
    ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
).stdout
for line in status.splitlines():
    code, path = line[:2], line[3:]
    if code.strip() == "M" and path in allowed:
        continue
    if code == "??":
        if "/src/" in path and path.startswith("packages/"):
            errors.append(f"untracked file would enter the artifacts: {path}")
        continue
    errors.append(f"tree differs from HEAD beyond the prepared READMEs: {line}")

# One version everywhere: both sources, the tag on HEAD, every artifact's
# file name and metadata.
versions: dict[str, str] = {}
for name, init in (
    ("scanmole", "packages/scanmole/src/scanmole/__init__.py"),
    ("scanmole-gui", "packages/scanmole-gui/src/scanmole_gui/__init__.py"),
):
    match = re.search(r'__version__ = "([^"]+)"', Path(init).read_text())
    if match is None:
        errors.append(f"cannot read __version__ from {init}")
    else:
        versions[name] = match.group(1)
version = versions.get("scanmole", "")
if len(set(versions.values())) != 1:
    errors.append(f"lockstep violated in the sources: {versions}")
tags = subprocess.run(
    ["git", "tag", "--points-at", "HEAD"], capture_output=True, text=True, check=True
).stdout.split()
if version and f"v{version}" not in tags:
    errors.append(
        f"no v{version} tag on HEAD (found: {tags or 'none'}); "
        "artifacts must be built from the tagged release state"
    )

def metadata_text(artifact: str) -> str:
    if artifact.endswith(".whl"):
        with zipfile.ZipFile(artifact) as bundle:
            meta = next(n for n in bundle.namelist() if n.endswith(".dist-info/METADATA"))
            return bundle.read(meta).decode("utf-8", errors="replace")
    with tarfile.open(artifact) as bundle:
        meta = next(n for n in bundle.getnames() if n.endswith("/PKG-INFO"))
        member = bundle.extractfile(meta)
        assert member is not None
        return member.read().decode("utf-8", errors="replace")

relative_link = re.compile(r"\]\((?!https?://|#|mailto:)")
for artifact in sys.argv[1:]:
    text = metadata_text(artifact)
    headers, _, description = text.partition("\n\n")
    meta_version = ""
    for line in headers.splitlines():
        if line.startswith("Version:"):
            meta_version = line.split(":", 1)[1].strip()
    if meta_version != version:
        errors.append(f"{artifact}: metadata version {meta_version} != {version}")
    if version and version not in Path(artifact).name:
        errors.append(f"{artifact}: file name does not carry version {version}")
    # The PyPI page is this description. A pointer stub means the README
    # preparation (copy over the member READMEs) was skipped; relative
    # links break on pypi.org.
    if len(description) < 5000:
        errors.append(
            f"{artifact}: description is {len(description)} chars; "
            "looks like the pointer README instead of the prepared project page"
        )
    hits = relative_link.findall(description)
    if hits:
        errors.append(
            f"{artifact}: description contains {len(hits)} relative link(s); "
            "run the README preparation (see DEVELOPMENT.md step 5)"
        )

if errors:
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    raise SystemExit(1)
print(f"consistent: {len(sys.argv) - 1} artifact(s) at {version}, tag v{version} on HEAD")
PY

    log "Install + smoke test of the artifacts (clean environment)"
    local venv="${WORK_DIR}/venv-artifacts"
    uv venv "$venv" >/dev/null
    uv pip install --python "$venv/bin/python" dist/*.whl >/dev/null
    "$venv/bin/${COMMAND_NAME}" --version >/dev/null
    local runtime_version meta_cli meta_gui
    runtime_version="$("$venv/bin/python" -c "import ${IMPORT_NAME}; print(${IMPORT_NAME}.__version__)")"
    meta_cli="$("$venv/bin/python" -c \
        "from importlib.metadata import version; print(version('scanmole'))")"
    meta_gui="$("$venv/bin/python" -c \
        "from importlib.metadata import version; print(version('scanmole-gui'))")"
    if [ "$meta_cli" != "$runtime_version" ] || [ "$meta_cli" != "$meta_gui" ]; then
        echo "error: version skew after install: runtime ${runtime_version}," \
             "scanmole ${meta_cli}, scanmole-gui ${meta_gui}" >&2
        exit 1
    fi
    echo "install ok: both packages at ${meta_cli}"
    log "Artifacts are ready to publish"
}

smoke_test_matrix() {
    local cli_wheel gui_wheel
    cli_wheel="$(ls -1 dist/scanmole-*.whl | head -n1)"
    gui_wheel="$(ls -1 dist/scanmole_gui-*.whl | head -n1)"
    if [ -z "$cli_wheel" ] || [ -z "$gui_wheel" ]; then
        echo "error: expected scanmole and scanmole_gui wheels in dist/" >&2
        exit 1
    fi

    local expected_version
    expected_version="$(uv run python -c "import ${IMPORT_NAME}; print(${IMPORT_NAME}.__version__)")"

    for py in "${SUPPORTED_PYTHONS[@]}"; do
        log "Install + smoke test on Python ${py} (clean environment)"
        local venv="${WORK_DIR}/venv-${py}"
        uv venv --python "$py" "$venv" >/dev/null
        # Install ONLY the built wheels (no project sources on the path).
        uv pip install --python "$venv/bin/python" "$cli_wheel" "$gui_wheel" >/dev/null

        # Import smoke test against the installed artifact.
        local installed_version
        installed_version="$(
            "$venv/bin/python" -c "import ${IMPORT_NAME}; print(${IMPORT_NAME}.__version__)"
        )"
        if [ "$installed_version" != "$expected_version" ]; then
            echo "error: installed version '${installed_version}' != source" \
                 "version '${expected_version}'" >&2
            exit 1
        fi
        echo "import ok: ${IMPORT_NAME} ${installed_version}"

        # The GUI package must stay importable without GTK (its launcher and
        # the pure helpers are GTK-free by design).
        "$venv/bin/python" -c "import scanmole_gui" >/dev/null
        echo "import ok: scanmole_gui"

        # Distribution metadata must agree with both runtime versions and
        # with the lockstep policy (one version for both packages). The
        # __version__ check above cannot see a stale pyproject version.
        local meta_cli meta_gui gui_runtime
        meta_cli="$("$venv/bin/python" -c \
            "from importlib.metadata import version; print(version('scanmole'))")"
        meta_gui="$("$venv/bin/python" -c \
            "from importlib.metadata import version; print(version('scanmole-gui'))")"
        gui_runtime="$("$venv/bin/python" -c \
            "import scanmole_gui; print(scanmole_gui.__version__)")"
        if [ "$meta_cli" != "$installed_version" ] \
            || [ "$meta_gui" != "$gui_runtime" ] \
            || [ "$meta_cli" != "$meta_gui" ]; then
            echo "error: version skew: scanmole metadata ${meta_cli}," \
                 "runtime ${installed_version}; scanmole-gui metadata" \
                 "${meta_gui}, runtime ${gui_runtime}" >&2
            exit 1
        fi
        echo "metadata ok: both packages at ${meta_cli}"

        # Command-line smoke test against the installed console scripts.
        "$venv/bin/${COMMAND_NAME}" --version >/dev/null
        "$venv/bin/${COMMAND_NAME}" --help >/dev/null
        echo "cli ok: ${COMMAND_NAME} --version / --help"

        # The GUI launcher must fail with its one-line install hint in a clean
        # environment (no PyGObject), not with an import traceback.
        local gui_output
        if gui_output="$("$venv/bin/${GUI_COMMAND_NAME}" 2>&1)"; then
            echo "error: ${GUI_COMMAND_NAME} unexpectedly succeeded without GTK" >&2
            exit 1
        fi
        if ! printf '%s' "$gui_output" | grep -q "PyGObject"; then
            echo "error: ${GUI_COMMAND_NAME} did not print the PyGObject hint:" >&2
            printf '%s\n' "$gui_output" >&2
            exit 1
        fi
        echo "gui ok: ${GUI_COMMAND_NAME} prints the install hint without GTK"
    done
}

main() {
    require_uv
    if [ "$ARTIFACTS_ONLY" = "yes" ]; then
        echo "Artifact validation for ${DIST_NAME}"
        validate_artifacts
        return
    fi
    echo "Release check for ${DIST_NAME}"
    echo "Python versions: ${SUPPORTED_PYTHONS[*]}"
    ensure_pythons
    run_static_checks
    run_tests_matrix
    build_artifacts
    smoke_test_matrix
    log "All release checks passed"
}

main
