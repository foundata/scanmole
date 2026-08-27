#!/usr/bin/env bash
#
# Regenerate the GUI/CLI screenshots under assets/images/screenshots/.
#
# Interactive and hardware-driven: this drives a real scanmole-gui window
# on the current desktop session and a real SANE scanner. It needs a real
# compositor (GNOME/mutter) for the rounded window decorations the
# existing screenshots show; a bare Xvfb has no compositor and renders
# square corners instead. It also cannot synthesize mouse clicks on a
# Wayland session (mutter does not route XTest input the way a plain X
# server does), so every step that needs a physical action (load paper,
# click Scan, open Settings, resize the window) prompts the operator,
# waits for Enter, then counts down out loud before capturing. The count-
# down exists because pressing Enter leaves the terminal focused, not the
# GUI: it is the window the operator switches to and acts in during that
# pause, not a delay for its own sake.
#
# Run this after any GUI layout change, and again before tagging a
# release if the layout or a shown device has changed since.
#
# Usage:
#   uv run scripts/update-screenshots.sh [--output-dir DIR]
#
# Run via "uv run" (or from an activated venv) so scanmole/scanmole-gui
# resolve to this checkout. Also requires: a connected SANE scanner,
# ImageMagick ("import", "magick"), xterm, and uv itself (used to fetch
# python-xlib on demand for window discovery; not a project dependency).
#
# Writes PNGs into --output-dir (default: assets/images/screenshots).
# Never stages or commits anything; review the result yourself.

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
set -u
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF_DIR
REPO_ROOT="$(cd "${SELF_DIR}/.." && pwd)"
readonly REPO_ROOT

OUTPUT_DIR="${REPO_ROOT}/assets/images/screenshots"

# How long the countdown after Enter runs by default, and for the one
# step (the scan) whose result also depends on processing time, not just
# the time to switch window and click.
readonly DEFAULT_COUNTDOWN=10
readonly SCAN_COUNTDOWN=35

# How many rows of "scanmole --help" count as its upper half, and the
# terminal size for the CLI scan demo (tall enough that a typical
# duplex batch's output does not scroll its start out of view).
readonly HELP_LINES=50
readonly CLI_COLUMNS=100
readonly CLI_SCAN_ROWS=45

GUI_PID=''
XTERM_PID=''
WORK_DIR=''
SCANMOLE_BIN=''
SCANMOLE_GUI_BIN=''

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
usage: uv run scripts/update-screenshots.sh [--output-dir DIR]

--output-dir DIR    write PNGs here (default: assets/images/screenshots)

Interactive: prompts guide you through the GUI (load paper, click Scan,
open Settings, resize the window) and count down after each Enter so you
have time to switch focus and act before the capture fires.
USAGE
  exit 2
}

###
# Kill the launched GUI/xterm and remove the scratch directory. Runs on
# any exit path so a failed or interrupted run never leaves either
# behind.
cleanup() {
  [ -n "${GUI_PID}" ] && kill "${GUI_PID}" 2>/dev/null
  [ -n "${XTERM_PID}" ] && kill "${XTERM_PID}" 2>/dev/null
  [ -n "${WORK_DIR}" ] && rm -rf "${WORK_DIR}"
  return 0
}

###
# Show an instruction, wait for Enter, then count down out loud. The
# operator presses Enter to acknowledge, switches focus to the GUI
# during the count, and performs the described action there; the
# capture happens only once the count reaches zero.
# Arguments:
#   $1 - The instruction to show.
#   $2 - Countdown length in seconds (default: DEFAULT_COUNTDOWN).
prompt_and_wait() {
  local instruction="$1" seconds="${2:-${DEFAULT_COUNTDOWN}}" remaining
  printf '\n>>> %s\n' "${instruction}"
  read -r -p '    press Enter, then switch to the window and do it... ' _
  for ((remaining = seconds; remaining > 0; remaining--)); do
    printf '\r    capturing in %2ds... ' "${remaining}"
    sleep 1
  done
  printf '\r    capturing now.          \n'
}

###
# Print "id abs_x abs_y width height" for the visible top-level window
# with the given WM name, or exit 1 if none is mapped.
#
# This only ever targets a regular top-level window, not a GTK popover
# (a dropdown's option list, a menu): a popover's background is
# semi-transparent, and a raw X11 window-pixmap read gets the
# premultiplied-alpha buffer straight, with no compositor behind it to
# blend against. Anywhere alpha is low, that reads back as flat black
# instead of the intended light, translucent background, so a popover
# cannot be captured correctly this way. Capturing only regular windows
# avoids that; there is no screenshot of an open dropdown/menu.
# Arguments:
#   $1 - The window's WM_NAME to look for.
find_window() {
  local name="$1"
  uv run --with python-xlib python3 - "${name}" <<'PYEOF'
import sys

from Xlib import X, display

target = sys.argv[1]
d = display.Display()
root = d.screen().root


def find_all(win, acc):
    try:
        name = win.get_wm_name()
    except Exception:
        name = None
    if name == target:
        acc.append(win)
    try:
        children = win.query_tree().children
    except Exception:
        return
    for child in children:
        find_all(child, acc)


def viewable(win):
    try:
        return win.get_attributes().map_state == X.IsViewable
    except Exception:
        return False


def is_popup(win):
    try:
        return bool(win.get_attributes().override_redirect)
    except Exception:
        return False


found = []
find_all(root, found)
candidates = [w for w in found if viewable(w) and not is_popup(w)]
if not candidates:
    sys.exit(1)

# The largest match: helper/tooltip windows can share the same WM name.
win = max(candidates, key=lambda w: w.get_geometry().width * w.get_geometry().height)

abs_x, abs_y = 0, 0
node = win
while True:
    geom = node.get_geometry()
    abs_x += geom.x
    abs_y += geom.y
    parent = node.query_tree().parent
    if parent is None or parent.id == root.id:
        break
    node = parent

final = win.get_geometry()
print(f"{win.id} {abs_x} {abs_y} {final.width} {final.height}")
PYEOF
}

###
# Capture one window by id into OUT, trimmed to its actual content.
#
# The raw X11 capture includes libadwaita's transparent CSD shadow
# margin, rendered as flat black instead of blended against the desktop
# (a plain window-pixmap read has no compositing behind it); trimming
# that margin is what leaves the rounded corners intact without a black
# frame around them.
# Arguments:
#   $1 - The window id, as printed by find_window.
#   $2 - The destination PNG path.
capture_window() {
  local win_id="$1" out="$2" raw="${WORK_DIR}/raw.png"
  import -window "${win_id}" "${raw}"
  magick "${raw}" -fuzz 1% -trim +repage "${out}"
}

###
# Find the named window and capture it into OUT. Exits 1 if the window
# is not mapped (e.g. it was closed).
# Arguments:
#   $1 - The WM_NAME to look for.
#   $2 - The destination PNG path.
find_and_capture() {
  local wm_name="$1" out="$2" info win_id
  info="$(find_window "${wm_name}")" || {
    err "window '${wm_name}' not found (closed?)"
    exit 1
  }
  win_id="${info%% *}"
  capture_window "${win_id}" "${out}"
  printf 'wrote %s\n' "${out}"
}

###
# Prompt, count down, then capture the current ScanMole window into
# OUTPUT_DIR/NAME. This is a required (non-skippable) step.
# Arguments:
#   $1 - Output filename (relative to OUTPUT_DIR).
#   $2 - The instruction to show before capturing.
#   $3 - Countdown length in seconds (default: DEFAULT_COUNTDOWN).
capture_named() {
  local name="$1" instruction="$2" seconds="${3:-${DEFAULT_COUNTDOWN}}"
  prompt_and_wait "${instruction}" "${seconds}"
  find_and_capture 'ScanMole' "${OUTPUT_DIR}/${name}"
}

###
# Print the real (non-virtual) SANE device identifiers scanmole sees,
# one per line.
list_real_devices() {
  "${SCANMOLE_BIN}" --list-devices --json 2>/dev/null \
    | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("event") != "devices":
        continue
    for device in event.get("devices", []):
        if "virtual" not in (device.get("type") or ""):
            print(device.get("device", ""))
'
}

###
# Run the CLI demo (device listing + a duplex scan) in a real terminal
# and capture it into OUTPUT_DIR/scanmole-cli-01-scan.png. The scan
# itself needs no further operator action once paper is loaded: the
# demo script signals completion with a sentinel file, which this polls
# for instead of guessing how long OCR will take.
capture_cli_scan() {
  local device demo_script sentinel win_id i
  device="$(list_real_devices | head -n1)"
  if [ -z "${device}" ]; then
    err 'no real scanner found; skipping scanmole-cli-01-scan.png'
    return 0
  fi

  demo_script="${WORK_DIR}/cli-scan-demo.sh"
  sentinel="${WORK_DIR}/cli-scan-done"
  cat >"${demo_script}" <<DEMO
#!/usr/bin/env bash
echo '\$ scanmole --list-devices'
'${SCANMOLE_BIN}' --list-devices
echo
echo '\$ scanmole -d "${device}" --source adf-duplex -o scan_demo.pdf'
'${SCANMOLE_BIN}' -d '${device}' --source adf-duplex -o '${WORK_DIR}/scan_demo.pdf'
touch '${sentinel}'
DEMO
  chmod +x "${demo_script}"

  prompt_and_wait 'Load a sheet for the CLI duplex demo (its terminal opens after the count).' \
    "${DEFAULT_COUNTDOWN}"
  xterm -title 'ScanMole CLI demo' -fa Monospace -fs 14 \
    -geometry "${CLI_COLUMNS}x${CLI_SCAN_ROWS}" -bg white -fg black -hold \
    -e "${demo_script}" &
  XTERM_PID=$!

  printf '    waiting for the scan to finish... '
  for ((i = 0; i < 120; i++)); do
    [ -f "${sentinel}" ] && break
    sleep 1
  done
  if [ ! -f "${sentinel}" ]; then
    err 'timed out waiting for the CLI scan; capturing whatever is on screen'
  fi
  printf 'done\n'

  local win_info
  if ! win_info="$(find_window 'ScanMole CLI demo')"; then
    err 'CLI demo terminal not found; skipping scanmole-cli-01-scan.png'
    kill "${XTERM_PID}" 2>/dev/null
    XTERM_PID=''
    return 0
  fi
  win_id="${win_info%% *}"
  capture_window "${win_id}" "${OUTPUT_DIR}/scanmole-cli-01-scan.png"
  printf 'wrote %s/scanmole-cli-01-scan.png\n' "${OUTPUT_DIR}"

  kill "${XTERM_PID}" 2>/dev/null
  XTERM_PID=''
}

###
# Capture "scanmole --help"'s upper half into
# OUTPUT_DIR/scanmole-cli-02-help.png. Fully scripted: nothing for the
# operator to do, so this needs no prompt.
capture_cli_help() {
  local help_script win_id
  help_script="${WORK_DIR}/cli-help-demo.sh"
  cat >"${help_script}" <<DEMO
#!/usr/bin/env bash
echo '\$ scanmole --help'
'${SCANMOLE_BIN}' --help | head -n ${HELP_LINES}
DEMO
  chmod +x "${help_script}"

  xterm -title 'ScanMole CLI help' -fa Monospace -fs 14 \
    -geometry "${CLI_COLUMNS}x$((HELP_LINES + 2))" -bg white -fg black -hold \
    -e "${help_script}" &
  XTERM_PID=$!
  sleep 2

  local win_info
  if ! win_info="$(find_window 'ScanMole CLI help')"; then
    err 'CLI help terminal not found; skipping scanmole-cli-02-help.png'
    kill "${XTERM_PID}" 2>/dev/null
    XTERM_PID=''
    return 0
  fi
  win_id="${win_info%% *}"
  capture_window "${win_id}" "${OUTPUT_DIR}/scanmole-cli-02-help.png"
  printf 'wrote %s/scanmole-cli-02-help.png\n' "${OUTPUT_DIR}"

  kill "${XTERM_PID}" 2>/dev/null
  XTERM_PID=''
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --output-dir)
        [ $# -ge 2 ] || usage
        OUTPUT_DIR="$2"
        shift 2
        ;;
      -h | --help) usage ;;
      *)
        err "unknown argument: $1"
        usage
        ;;
    esac
  done

  command -v import >/dev/null 2>&1 || {
    err 'ImageMagick "import" not found'
    exit 1
  }
  command -v magick >/dev/null 2>&1 || {
    err 'ImageMagick "magick" not found'
    exit 1
  }
  command -v xterm >/dev/null 2>&1 || {
    err 'xterm not found'
    exit 1
  }
  command -v uv >/dev/null 2>&1 || {
    err 'uv not found (needed for python-xlib)'
    exit 1
  }
  SCANMOLE_BIN="$(command -v scanmole)" || {
    err 'scanmole not on PATH (run via "uv run")'
    exit 1
  }
  SCANMOLE_GUI_BIN="$(command -v scanmole-gui)" || {
    err 'scanmole-gui not on PATH (run via "uv run")'
    exit 1
  }
  readonly SCANMOLE_BIN SCANMOLE_GUI_BIN

  export DISPLAY="${DISPLAY:-:0}"
  mkdir -p "${OUTPUT_DIR}"
  WORK_DIR="$(mktemp -d)"
  trap cleanup EXIT

  printf 'devices seen by scanmole:\n'
  "${SCANMOLE_BIN}" --list-devices || true

  printf '\nlaunching scanmole-gui on DISPLAY=%s ...\n' "${DISPLAY}"
  GDK_BACKEND=x11 "${SCANMOLE_GUI_BIN}" >"${WORK_DIR}/gui.log" 2>&1 &
  GUI_PID=$!
  sleep 3

  # The default window width already renders the two-column layout;
  # there is no separate "two scanners" shot to gate on device count.
  capture_named 'scanmole-gui-01-main.png' \
    'Bring the ScanMole window to the front, ready to scan (no dialog open).'

  capture_named 'scanmole-gui-02-scan-result.png' \
    'Load a sheet, click Scan in the GUI, and wait for it to finish (result bar visible).' \
    "${SCAN_COUNTDOWN}"

  capture_named 'scanmole-gui-03-settings.png' \
    'Click "Settings" in the GUI to open the dialog.'

  # The narrow, single-column layout is the outlier compared to the
  # default width above, so it is captured last among the GUI shots.
  capture_named 'scanmole-gui-04-narrow.png' \
    'Close Settings and narrow the window until it switches to a single column.'

  capture_cli_scan
  capture_cli_help

  printf '\ndone. Review the files under %s before staging/committing them.\n' "${OUTPUT_DIR}"
}

main "$@"
