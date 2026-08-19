#!/usr/bin/env bash
#
# Raw scanner evidence capture for the ScanMole evidence kit.
#
# Bash required for: arrays (exact argv forwarding of backend options).
#
# Captures raw PNM frames straight from scanimage, with no ScanMole
# processing, into a run directory under an external evidence root that
# must lie outside any Git worktree. The wrapper owns device, source,
# mode, resolution, the PNM output format and the batch destination;
# everything backend-specific (geometry, ALD, enhancement controls)
# travels as ordered arguments after "--" exactly as given. See
# README.md in this directory for the runbook.
#
# Usage:
#   capture.sh --output-root DIR --device-label LABEL --device SANE_ID \
#     --run RUN_ID --source SOURCE --mode MODE --resolution DPI \
#     --paper TEXT --orientation TEXT [-- SCANIMAGE_ARG...]

set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF_DIR

###
# Print an error message to STDERR.
# Arguments:
#   $@ - The message.
err() {
  printf 'error: %s\n' "$*" >&2
}

###
# Print usage to STDERR and exit 2.
usage() {
  cat >&2 <<'USAGE'
usage: capture.sh --output-root DIR --device-label LABEL --device SANE_ID \
         --run RUN_ID --source SOURCE --mode MODE --resolution DPI \
         --paper TEXT --orientation TEXT [-- SCANIMAGE_ARG...]

All options are required. Backend-specific scanimage options (geometry,
--ald, enhancement controls) follow "--" and are forwarded in order,
exactly as given. See README.md in this directory.
USAGE
  exit 2
}

###
# Reject forwarded scanimage arguments that override wrapper-owned ones.
# Arguments:
#   $@ - The forwarded arguments.
# Returns:
#   0 if all arguments are permitted, exits 2 otherwise.
check_forwarded() {
  local argument
  for argument in "$@"; do
    case "${argument}" in
      -d | -d* | --device-name | --device-name=*)
        err "forwarded '${argument}' would override the wrapper-owned device"
        exit 2
        ;;
      --source | --source=* | --mode | --mode=* | --resolution | --resolution=*)
        err "forwarded '${argument}' would override a wrapper-owned option;" \
          'use the dedicated wrapper option instead'
        exit 2
        ;;
      --format | --format=*)
        err "forwarded '${argument}' would override the owned PNM format"
        exit 2
        ;;
      -b | -b* | --batch | --batch=* | --batch-start | --batch-start=* \
        | --batch-count | --batch-count=* | --batch-increment \
        | --batch-increment=* | --batch-double | --batch-print \
        | -o | -o* | --output-file | --output-file=*)
        err "forwarded '${argument}' would override the owned batch output"
        exit 2
        ;;
      -A | --all-options | -L | --list-devices | -h | --help | -V | --version \
        | -n | --dont-scan | -T | --test | -f | --formatted-device-list \
        | --batch-prompt | --batch-prompt=*)
        err "forwarded '${argument}' would change the operation; this tool only scans"
        exit 2
        ;;
    esac
  done
}

###
# Reject control characters in a line-oriented metadata value.
# Arguments:
#   $1 - The option name (for the error message).
#   $2 - The value.
check_scalar() {
  case "$2" in
    *$'\n'* | *$'\r'* | *$'\t'*)
      err "option $1 must not contain newline, carriage-return or tab characters"
      exit 2
      ;;
  esac
}

###
# Refuse a canonicalized path whose nearest existing ancestor lies in a
# Git worktree. Symlink components are resolved first, so neither the
# root nor a pre-existing label/runs symlink can smuggle raw evidence
# into a repository. A hostile concurrent symlink swap between this
# check and the mkdir is outside this local tool's threat model.
# Arguments:
#   $1 - The already canonicalized absolute path.
#   $2 - A short description for the error message.
refuse_worktree_path() {
  local probe="$1"
  while [ ! -d "${probe}" ] && [ "${probe}" != '/' ]; do
    probe="$(dirname -- "${probe}")"
  done
  if git -C "${probe}" rev-parse --show-toplevel >/dev/null 2>&1; then
    err "$2 resolves into a Git worktree ($1);" \
      'raw evidence must stay outside Git permanently'
    exit 2
  fi
}

###
# Refuse an output root that resolves into any Git worktree.
# Symlinks are resolved first, and for a not-yet-existing path the
# nearest existing ancestor decides, so a symlink or fresh subdirectory
# cannot smuggle raw evidence into a repository.
# Arguments:
#   $1 - The requested output root.
# Outputs:
#   The resolved absolute path on STDOUT.
resolve_output_root() {
  local requested="$1" resolved
  resolved="$(realpath -m -- "${requested}")" || exit 2
  refuse_worktree_path "${resolved}" "output root '${requested}'"
  printf '%s\n' "${resolved}"
}

###
# Finalize the run metadata with a status line, preserving the caller's
# exit status. Safe to call more than once; only the first status wins.
# Globals:
#   metadata_file, run_dir
# Arguments:
#   $1 - The status text to record.
finalize_status() {
  local status_text="$1"
  if [ -n "${metadata_file:-}" ] && [ -f "${metadata_file}" ] \
    && grep -q '^status: RUNNING$' "${metadata_file}"; then
    sed -i "s/^status: RUNNING$/status: ${status_text}/" "${metadata_file}"
  fi
}

###
# Inventory the completed pages of the run.
# Globals:
#   run_dir, SELF_DIR
# Returns:
#   0 when every page is a valid PNM (trailing raster bytes beyond the
#   declared geometry are valid and merely reported), 1 otherwise.
inventory_pages() {
  local pages=() inventory_exit=0
  while IFS= read -r -d '' page; do
    pages+=("${page}")
  done < <(find "${run_dir}" -maxdepth 1 -name 'page_*.pnm' -print0 | sort -z)
  if [ "${#pages[@]}" -gt 0 ]; then
    python3 "${SELF_DIR}/pnm_inventory.py" "${pages[@]}" \
      > "${run_dir}/inventory.tsv" \
      2>> "${run_dir}/inventory-errors.txt" || inventory_exit=1
  fi
  return "${inventory_exit}"
}

###
# Main entry point.
# Arguments:
#   $@ - Command-line arguments.
main() {
  local output_root='' device_label='' device='' run_id='' source_name=''
  local mode='' resolution='' paper='' orientation=''
  local forwarded=()

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --output-root | --device-label | --device | --run | --source | --mode \
        | --resolution | --paper | --orientation)
        if [ "$#" -lt 2 ]; then
          err "option $1 requires a value"
          exit 2
        fi
        ;;
      --) shift; forwarded=("$@"); break ;;
      *) err "unknown option '$1'"; usage ;;
    esac
    case "$1" in
      --output-root) output_root="$2" ;;
      --device-label) device_label="$2" ;;
      --device) device="$2" ;;
      --run) run_id="$2" ;;
      --source) source_name="$2" ;;
      --mode) mode="$2" ;;
      --resolution) resolution="$2" ;;
      --paper) paper="$2" ;;
      --orientation) orientation="$2" ;;
    esac
    shift 2
  done

  local entry name option
  for entry in output_root:output-root device_label:device-label \
    device:device run_id:run source_name:source mode:mode \
    resolution:resolution paper:paper orientation:orientation; do
    name="${entry%%:*}"
    option="${entry#*:}"
    if [ -z "${!name}" ]; then
      err "missing required option --${option}"
      usage
    fi
  done
  if ! printf '%s' "${device_label}" | grep -Eq '^[a-z0-9][a-z0-9-]*$'; then
    err "device label '${device_label}' must be lowercase [a-z0-9-] (a path component)"
    exit 2
  fi
  if ! printf '%s' "${run_id}" | grep -Eq '^[a-z0-9][a-z0-9._-]*$'; then
    err "run id '${run_id}' must be [a-z0-9._-] (a path component)"
    exit 2
  fi
  if ! printf '%s' "${resolution}" | grep -Eq '^[0-9]+$' || [ "${resolution}" -lt 1 ]; then
    err "resolution '${resolution}' must be a positive integer (dpi)"
    exit 2
  fi
  check_scalar '--device' "${device}"
  check_scalar '--source' "${source_name}"
  check_scalar '--mode' "${mode}"
  check_scalar '--paper' "${paper}"
  check_scalar '--orientation' "${orientation}"
  check_forwarded ${forwarded[@]+"${forwarded[@]}"}

  local resolved_root
  resolved_root="$(resolve_output_root "${output_root}")" || exit 2

  # Canonicalize the final run directory too: a pre-existing label or
  # runs symlink below a safe root could otherwise route the raw
  # evidence into a repository.
  run_dir="$(realpath -m -- "${resolved_root}/${device_label}/runs/${run_id}")"
  refuse_worktree_path "${run_dir}" "run directory for '${run_id}'"
  if [ -e "${run_dir}" ]; then
    err "run '${run_id}' already exists: ${run_dir} (runs are never overwritten)"
    exit 1
  fi
  mkdir -p "${run_dir}"

  # The wrapper-owned options come first; the ordered backend-specific
  # arguments follow exactly as received (SANE applies options in argv
  # order, which matters for geometry that re-ranges dependent options).
  local command=(scanimage -d "${device}" --source "${source_name}"
    --mode "${mode}" --resolution "${resolution}")
  command+=(${forwarded[@]+"${forwarded[@]}"})
  command+=(--format=pnm "--batch=${run_dir}/page_%04d.pnm" --batch-print)

  # Metadata before acquisition, so even an aborted run is documented.
  # This file stays outside Git permanently: the raw device identifier
  # and external paths in it must never reach repository fixtures.
  metadata_file="${run_dir}/metadata.txt"
  {
    printf 'device_label: %s\n' "${device_label}"
    printf 'device: %s\n' "${device}"
    printf 'source: %s\n' "${source_name}"
    printf 'mode: %s\n' "${mode}"
    printf 'resolution: %s\n' "${resolution}"
    printf 'paper: %s\n' "${paper}"
    printf 'orientation: %s\n' "${orientation}"
    printf 'command:'
    printf ' %q' "${command[@]}"
    printf '\n'
    printf 'scanimage: %s\n' "$(scanimage --version 2>&1 | head -n 1)"
    printf 'started: %s\n' "$(date -Iseconds)"
    printf 'status: RUNNING\n'
  } > "${metadata_file}"

  # An interrupt records the truth, inventories the completed pages and
  # then re-raises the signal so the original exit status is preserved.
  trap 'finalize_status "INTERRUPTED (SIGINT)"; inventory_pages; trap - INT; kill -INT "$$"' INT
  trap 'finalize_status "INTERRUPTED (SIGTERM)"; inventory_pages; trap - TERM; kill -TERM "$$"' TERM

  local scan_exit=0
  "${command[@]}" \
    > "${run_dir}/scanimage-stdout.txt" \
    2> "${run_dir}/scanimage-stderr.txt" || scan_exit="$?"
  trap - INT TERM

  local frames
  frames="$(find "${run_dir}" -maxdepth 1 -name 'page_*.pnm' | wc -l)"
  # The inventory runs before the status is composed: a scan is only
  # trustworthy evidence when every delivered frame parses (trailing
  # raster bytes beyond the declared geometry are valid and reported).
  local inventory_ok=0
  inventory_pages || inventory_ok=1

  # Exit 7 (SANE_STATUS_NO_DOCS: the feeder ran empty) is the normal end
  # of a batch once at least one frame was delivered.
  local status_text scan_ok=1
  if [ "${scan_exit}" -eq 0 ] \
    || { [ "${scan_exit}" -eq 7 ] && [ "${frames}" -gt 0 ]; }; then
    scan_ok=0
  fi
  if [ "${scan_ok}" -ne 0 ]; then
    # Acquisition failure stays the primary cause; an invalid inventory
    # is recorded alongside it instead of replacing it.
    status_text="FAILED (scanimage exit ${scan_exit}, ${frames} frame(s)"
    [ "${inventory_ok}" -ne 0 ] && status_text="${status_text}; inventory invalid"
    status_text="${status_text})"
  elif [ "${frames}" -eq 0 ]; then
    # A clean exit without a single delivered frame is not usable
    # evidence and must not read as a completed run in a corpus sweep.
    status_text="INCOMPLETE (scanimage exit ${scan_exit}, 0 frame(s), nothing delivered)"
  elif [ "${inventory_ok}" -ne 0 ]; then
    status_text="INCOMPLETE (scanimage exit ${scan_exit}, ${frames} frame(s), invalid frame(s) in inventory)"
  else
    status_text="completed (scanimage exit ${scan_exit}, ${frames} frame(s))"
  fi
  finalize_status "${status_text}"

  if [ "${scan_ok}" -ne 0 ]; then
    err "scanimage exit ${scan_exit}, ${frames} frame(s) preserved;" \
      "see ${run_dir}/scanimage-stderr.txt"
    exit 1
  fi
  if [ "${frames}" -eq 0 ]; then
    err "run '${run_id}' is INCOMPLETE: scanimage exited ${scan_exit}" \
      'without delivering a frame (all files preserved)'
    exit 1
  fi
  if [ "${inventory_ok}" -ne 0 ]; then
    err "run '${run_id}' is INCOMPLETE: invalid frame(s), see" \
      "${run_dir}/inventory-errors.txt (all files preserved)"
    exit 1
  fi
  printf 'done: run %s (%s frame(s)) in %s\n' "${run_id}" "${frames}" "${run_dir}"
}

main "$@"
