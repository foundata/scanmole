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
#
# Without arguments the supported version matrix below is used.

set -euo pipefail

# Temp environments live under $TMPDIR, often on a different filesystem than
# the uv cache; copy instead of hardlink to avoid a noisy fallback warning.
export UV_LINK_MODE=copy

# Supported Python versions (keep in sync with pyproject and the README).
# Override by passing versions as arguments.
SUPPORTED_PYTHONS=("3.12" "3.13" "3.14")
if [ "$#" -gt 0 ]; then
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
