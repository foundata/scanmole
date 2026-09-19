#!/usr/bin/env bash
# Bash required for: arrays (the version matrix) and BASH_SOURCE.
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
#
# The release artifacts themselves are built and validated by
# "uv run release build", which exports the committed revision, prepares the
# READMEs that ship in them and records their digests. See DEVELOPMENT.md.

# Consistent environment for predictable tool and shell behavior.
export PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin}"
if command -v locale >/dev/null 2>&1; then
  for locale_candidate in 'C.UTF-8' 'C.utf8' 'en_US.UTF-8' 'UTF-8' 'C'; do
    if LC_ALL="${locale_candidate}" locale charmap >/dev/null 2>&1; then
      export LC_ALL="${locale_candidate}"
      break
    fi
  done
else
  export LC_ALL='C'
fi
readonly LC_ALL
unset locale_candidate

# Against the style guide's default, and deliberately: this is a gate of
# roughly fifty commands where any single failure must stop the release,
# so an abort-by-default is worth more here than the edge cases set -e is
# rightly criticised for. The three options below close the ones that
# would otherwise let a failure through, and every check whose exit
# status carries meaning is still tested explicitly.
set -e
set -u
set -o pipefail
shopt -s inherit_errexit

# Temp environments live under ${TMPDIR}, often on a different filesystem than
# the uv cache; copy instead of hardlink to avoid a noisy fallback warning.
export UV_LINK_MODE='copy'

# Supported Python versions (keep in sync with pyproject and the README).
# Arguments override them; main() parses that.
supported_pythons=('3.12' '3.13' '3.14')

# Resolve the package directory (this script lives in <pkg>/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PKG_DIR="$(dirname "${SCRIPT_DIR}")"
readonly PKG_DIR
cd "${PKG_DIR}"

# Every shell script this repository ships, by dialect, with the option
# sets the shell style guide prescribes for each tool.
readonly -a POSIX_SCRIPTS=(
  'packages/scanmole-gui/po/buildmo.sh'
  'packages/scanmole-gui/po/genpot.sh'
  'packages/scanmole-gui/po/updatepo.sh'
)
readonly -a BASH_SCRIPTS=(
  'scripts/release-check.sh'
  'scripts/scanner-evidence/capture.sh'
  'scripts/update-screenshots.sh'
)
readonly -a SHFMT_OPTS_COMMON=(
  '--indent' '2' '--case-indent' '--binary-next-line' '--simplify'
)
readonly -a SHELLCHECK_OPTS_COMMON=(
  '--severity=style' '--exclude=SC2292' '--exclude=SC3040'
  '--exclude=SC3043' '--enable=all'
)

# Expected distribution and import names.
readonly DIST_NAME='scanmole'
readonly IMPORT_NAME='scanmole'
readonly COMMAND_NAME='scanmole'
readonly GUI_COMMAND_NAME='scanmole-gui'

WORK_DIR="$(mktemp -d)"
readonly WORK_DIR
trap 'rm -rf "${WORK_DIR}"; git -C "${PKG_DIR}" worktree prune >/dev/null 2>&1 || true' EXIT

###
# Announce the step that follows.
# Arguments:
#   $@ - The step description.
# Outputs:
#   Writes a blank-line-separated banner to STDOUT.
log() { printf '\n=== %s ===\n' "$*"; }

###
# Abort unless every named tool is available.
# Arguments:
#   $@ - The commands this gate is about to drive.
# Outputs:
#   Writes an error naming the first missing tool to STDERR.
# Returns:
#   0 when all are present, exits 1 otherwise.
require_tools() {
  local command_name
  for command_name in "$@"; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      printf "error: '%s' is required but not found in PATH\n" "${command_name}" >&2
      exit 1
    fi
  done
}

###
# Check every shipped shell script with the tools and the exact options
# the shell style guide prescribes, so the style holds without anyone
# remembering to run them. The dialect parse is part of it: a script can
# lint clean and still not parse under the shell in its shebang.
# Globals:
#   BASH_SCRIPTS, POSIX_SCRIPTS, SHELLCHECK_OPTS_COMMON
# Returns:
#   0 when every script passes, non-zero otherwise.
check_shell_scripts() {
  log "Shell scripts (shfmt, shellcheck, syntax)"
  local script
  for script in "${POSIX_SCRIPTS[@]}"; do
    shfmt --language-dialect posix "${SHFMT_OPTS_COMMON[@]}" --diff "${script}"
    shellcheck --shell=sh "${SHELLCHECK_OPTS_COMMON[@]}" "${script}"
    sh -n "${script}"
  done
  for script in "${BASH_SCRIPTS[@]}"; do
    shfmt --language-dialect bash "${SHFMT_OPTS_COMMON[@]}" --diff "${script}"
    shellcheck --shell=bash "${SHELLCHECK_OPTS_COMMON[@]}" "${script}"
    bash -n "${script}"
  done
}

###
# Make sure every supported interpreter is available so the matrix can
# actually run. `uv python install` is idempotent and a no-op when the
# version is already present.
# Globals:
#   supported_pythons
ensure_pythons() {
  log "Ensure Python interpreters: ${supported_pythons[*]}"
  uv python install "${supported_pythons[@]}"
}

###
# Run the formatter, linter and type checker once. They are
# version-independent here, because mypy targets the project minimum
# via pyproject.
run_static_checks() {
  log "Static checks (format, lint, type check)"
  uv run ruff format --check packages tests scripts/scanner-evidence
  uv run ruff check packages tests scripts/scanner-evidence
  uv run mypy packages/scanmole/src packages/scanmole-gui/src tests \
    scripts/scanner-evidence
  uv run python scripts/scanner-evidence/print_pack.py --check
}

###
# Run the test suite on every supported interpreter.
# Globals:
#   supported_pythons
run_tests_matrix() {
  for py in "${supported_pythons[@]}"; do
    log "Tests on Python ${py}"
    uv run --python "${py}" --isolated pytest -q
  done
}

###
# Build the wheels and source distributions from a pristine checkout of
# HEAD: the developer tree carries ignored litter (tool caches, editor
# droppings) that must never decide what ships. Local uncommitted changes
# are deliberately not built; a release is a commit, not a working tree.
# Globals:
#   PKG_DIR, WORK_DIR
build_artifacts() {
  log "Build wheels and source distributions (clean checkout of HEAD)"
  local tree_status
  tree_status="$(git status --porcelain)"
  if [ -n "${tree_status}" ]; then
    printf 'note: local changes present; artifacts are built from HEAD without them\n'
  fi
  local clean_dir="${WORK_DIR}/clean-src"
  git worktree add --detach --quiet "${clean_dir}" HEAD
  rm -rf dist
  (cd "${clean_dir}" && uv build --all-packages --out-dir "${PKG_DIR}/dist")
  git worktree remove --force "${clean_dir}"
  ls -1 dist
  check_artifact_hygiene
  check_lockstep_bound dist/*
}

###
# Check that scanmole-gui's dependency lower bound equals its own
# version. Releases are lockstep and the GUI/CLI handshake is directional
# (a newer GUI refuses an older engine), so the bound must agree in the
# sources and in every built GUI artifact.
# Arguments:
#   $@ - The artifacts to inspect.
# Returns:
#   0 when every bound agrees, 1 otherwise.
check_lockstep_bound() {
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

###
# Refuse release artifacts carrying caches or bytecode.
# Returns:
#   0 when every artifact in dist/ is clean, 1 otherwise.
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

###
# Validate dist/ exactly as it lies there, before publishing. The rebuild
# after the version bump and the README preparation happens from the
# working tree on purpose (the prepared READMEs only exist there), so
# these are the only checks the uploaded bytes ever get.
# Globals:
#   COMMAND_NAME, IMPORT_NAME, WORK_DIR
# Returns:
#   0 when the artifacts are publishable, exits 1 otherwise.
###
# Install the built wheels into a clean environment per supported
# interpreter and smoke-test the installed artifact, never the sources.
# Globals:
#   COMMAND_NAME, GUI_COMMAND_NAME, IMPORT_NAME, supported_pythons, WORK_DIR
# Returns:
#   0 when every interpreter passes, exits 1 otherwise.
smoke_test_matrix() {
  # An unmatched glob stays literal, so the -f tests below are what
  # actually decide whether the wheels are there.
  local -a cli_wheels=(dist/scanmole-*.whl) gui_wheels=(dist/scanmole_gui-*.whl)
  local cli_wheel="${cli_wheels[0]}" gui_wheel="${gui_wheels[0]}"
  if [ ! -f "${cli_wheel}" ] || [ ! -f "${gui_wheel}" ]; then
    printf 'error: expected scanmole and scanmole_gui wheels in dist/\n' >&2
    exit 1
  fi

  local expected_version
  expected_version="$(uv run python -c "import ${IMPORT_NAME}; print(${IMPORT_NAME}.__version__)")"

  for py in "${supported_pythons[@]}"; do
    log "Install + smoke test on Python ${py} (clean environment)"
    local venv="${WORK_DIR}/venv-${py}"
    uv venv --python "${py}" "${venv}" >/dev/null
    # Install ONLY the built wheels (no project sources on the path).
    uv pip install --python "${venv}/bin/python" "${cli_wheel}" "${gui_wheel}" >/dev/null

    # Import smoke test against the installed artifact.
    local installed_version
    installed_version="$(
      "${venv}/bin/python" -c "import ${IMPORT_NAME}; print(${IMPORT_NAME}.__version__)"
    )"
    if [ "${installed_version}" != "${expected_version}" ]; then
      printf "error: installed version '%s' != source version '%s'\n" \
        "${installed_version}" "${expected_version}" >&2
      exit 1
    fi
    printf 'import ok: %s %s\n' "${IMPORT_NAME}" "${installed_version}"

    # The GUI package must stay importable without GTK (its launcher and
    # the pure helpers are GTK-free by design).
    "${venv}/bin/python" -c "import scanmole_gui" >/dev/null
    printf 'import ok: scanmole_gui\n'

    # Distribution metadata must agree with both runtime versions and
    # with the lockstep policy (one version for both packages). The
    # __version__ check above cannot see a stale pyproject version.
    local meta_cli meta_gui gui_runtime
    meta_cli="$("${venv}/bin/python" -c \
      "from importlib.metadata import version; print(version('scanmole'))")"
    meta_gui="$("${venv}/bin/python" -c \
      "from importlib.metadata import version; print(version('scanmole-gui'))")"
    gui_runtime="$("${venv}/bin/python" -c \
      "import scanmole_gui; print(scanmole_gui.__version__)")"
    if [ "${meta_cli}" != "${installed_version}" ] \
      || [ "${meta_gui}" != "${gui_runtime}" ] \
      || [ "${meta_cli}" != "${meta_gui}" ]; then
      printf 'error: version skew: scanmole metadata %s, runtime %s; scanmole-gui metadata %s, runtime %s\n' \
        "${meta_cli}" "${installed_version}" "${meta_gui}" "${gui_runtime}" >&2
      exit 1
    fi
    printf 'metadata ok: both packages at %s\n' "${meta_cli}"

    # Command-line smoke test against the installed console scripts.
    "${venv}/bin/${COMMAND_NAME}" --version >/dev/null
    "${venv}/bin/${COMMAND_NAME}" --help >/dev/null
    printf 'cli ok: %s --version / --help\n' "${COMMAND_NAME}"

    # The GUI launcher must fail with its one-line install hint in a clean
    # environment (no PyGObject), not with an import traceback.
    local gui_output
    if gui_output="$("${venv}/bin/${GUI_COMMAND_NAME}" 2>&1)"; then
      printf 'error: %s unexpectedly succeeded without GTK\n' "${GUI_COMMAND_NAME}" >&2
      exit 1
    fi
    if ! printf '%s' "${gui_output}" | grep -q "PyGObject"; then
      printf 'error: %s did not print the PyGObject hint:\n' "${GUI_COMMAND_NAME}" >&2
      printf '%s\n' "${gui_output}" >&2
      exit 1
    fi
    printf 'gui ok: %s prints the install hint without GTK\n' "${GUI_COMMAND_NAME}"
  done
}

###
# Main entry point.
# Globals:
#   supported_pythons
# Arguments:
#   $@ - Command-line arguments: an optional Python version matrix.
main() {
  if [ "$#" -gt 0 ]; then
    supported_pythons=("$@")
  fi

  require_tools 'uv' 'shellcheck' 'shfmt'
  printf 'Release check for %s\n' "${DIST_NAME}"
  printf 'Python versions: %s\n' "${supported_pythons[*]}"
  ensure_pythons
  run_static_checks
  check_shell_scripts
  run_tests_matrix
  build_artifacts
  smoke_test_matrix
  log "All release checks passed"
}

main "$@"
