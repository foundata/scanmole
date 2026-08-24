"""Interactive GUI test: Ctrl+C must end scanmole-gui cleanly.

Runs the real ``scanmole-gui`` on a private D-Bus session (which also
sidesteps GApplication's single-instance forwarding to a running desktop
instance) and interrupts it. Needs a display and the dbus tooling, so it
skips itself on headless machines.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


class _PreviewFlowDouble:
    """Stands in for the preview flow: the window only asks it or stops it."""

    def __init__(self) -> None:
        self.requests = 0
        self.stopped = False

    def request(self) -> None:
        self.requests += 1

    def stop(self) -> None:
        self.stopped = True


pytestmark = pytest.mark.integration

# The gi check matters for the release matrix: its isolated venvs have a
# scanmole-gui on PATH, but no PyGObject, so the launcher (correctly) exits
# with the install hint instead of starting a GUI.
_NEEDS_DESKTOP = pytest.mark.skipif(
    shutil.which("dbus-run-session") is None
    or shutil.which("scanmole-gui") is None
    or importlib.util.find_spec("gi") is None
    or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs dbus-run-session, scanmole-gui, PyGObject and a display",
)


_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None, reason="needs PyGObject"
)


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_stale_runner_stderr_is_ignored() -> None:
    # The stderr handler must drop lines from an old runner exactly like
    # the stdout and exit handlers do; only the active runner may log.
    # Importing the module needs PyGObject but no display, and the handler
    # is exercised unbound on a duck-typed stand-in.
    from scanmole_gui.app import MainWindow

    class Window:
        def __init__(self) -> None:
            self._runner = object()
            self.lines: list[str] = []

        def _append_log(self, text: str) -> None:
            self.lines.append(text)

    window = Window()
    stale = object()

    MainWindow._on_stderr_line(window, stale, "stale noise\n")  # type: ignore[arg-type]
    assert window.lines == []

    MainWindow._on_stderr_line(window, window._runner, "live line\n")  # type: ignore[arg-type]
    assert window.lines == ["live line"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_application_shutdown_delegates_to_the_synchronous_barrier() -> None:
    # Once application shutdown begins, GLib sources may never fire again;
    # the handler must call the window's synchronous shutdown path, not the
    # timer-based close-request escalation.
    from scanmole_gui.app import ScanMoleApp

    calls: list[str] = []

    class Window:
        def _shutdown_now(self) -> None:
            calls.append("shutdown_now")

    class Props:
        active_window = Window()

    class App:
        props = Props()

    ScanMoleApp._on_shutdown(App())  # type: ignore[arg-type]
    assert calls == ["shutdown_now"]

    class GoneProps:
        active_window = None

    class GoneApp:
        props = GoneProps()
        window = None

    ScanMoleApp._on_shutdown(GoneApp())  # type: ignore[arg-type]
    assert calls == ["shutdown_now"]  # no window left: nothing to do


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_shutdown_now_persists_and_stops_the_runner_synchronously() -> None:
    from scanmole_gui.app import MainWindow

    class Runner:
        def __init__(self) -> None:
            self.shutdowns = 0

        def shutdown(self) -> None:
            self.shutdowns += 1

    class DeviceFlowDouble:
        def __init__(self) -> None:
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

    class Window:
        _shutdown_now = MainWindow._shutdown_now

        def __init__(self) -> None:
            self.persisted = 0
            self._preview = _PreviewFlowDouble()
            self._released = False
            self._deviceflow = DeviceFlowDouble()
            self._runner: Runner | None = Runner()

        def _persist_ui_state(self) -> None:
            self.persisted += 1

    window = Window()
    window._shutdown_now()  # type: ignore[misc]
    assert window.persisted == 1
    assert window._deviceflow.stops == 1  # device work released for good
    assert window._runner is not None and window._runner.shutdowns == 1
    # The preview owns a debounce, a worker and a directory monitor, none
    # of which may outlive the main loop that would have run them.
    assert window._preview.stopped is True

    idle = Window()
    idle._runner = None
    idle._shutdown_now()  # type: ignore[misc]
    assert idle.persisted == 1  # state persists even without a scan


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_view_state_wiring_follows_what_the_runtime_can_deliver() -> None:
    # Suspension notification exists from GTK 4.12; the window connects
    # it only where the runtime has it, and older runtimes keep working
    # with the visibility and focus signals alone.
    from scanmole_gui.app import MainWindow

    class Window:
        _watch_view_state = MainWindow._watch_view_state

        def __init__(self, *, suspendable: bool) -> None:
            self.connected: list[str] = []
            if suspendable:
                self.is_suspended = lambda: False
            self._on_visible_changed = lambda *a: None
            self._on_active_changed = lambda *a: None
            self._on_suspended_changed = lambda *a: None

        def connect(self, signal: str, handler: object) -> None:
            self.connected.append(signal)

    modern: Any = Window(suspendable=True)
    modern._watch_view_state()
    assert modern.connected == [
        "notify::visible",
        "notify::is-active",
        "notify::suspended",
    ]

    older: Any = Window(suspendable=False)
    older._watch_view_state()  # no error, and nothing it cannot deliver
    assert older.connected == ["notify::visible", "notify::is-active"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_start_predicate_requires_a_device_outside_a_search() -> None:
    # The one Start predicate: without a selected device, or while a
    # search runs, clicking Scan could only repeat discovery and fail.
    from scanmole_gui.app import MainWindow

    class Form:
        def __init__(self) -> None:
            self.enabled: bool | None = None

        def set_scan_enabled(self, enabled: bool) -> None:
            self.enabled = enabled

    class DeviceFlowDouble:
        def __init__(self) -> None:
            self.searching = False
            self.cli_blocked = False

    class Window:
        _update_scan_enabled = MainWindow._update_scan_enabled
        _scan_allowed = MainWindow._scan_allowed

        def __init__(self) -> None:
            self._form = Form()
            self._runner = None
            self._deviceflow = DeviceFlowDouble()
            self._selection_block_reason: str | None = None
            self.device: str | None = "sane:0"

        def _selected_device(self) -> str | None:
            return self.device

    window = Window()
    window._update_scan_enabled()  # type: ignore[misc]
    assert window._form.enabled is True  # idle, device selected

    def searching(window: Any) -> None:
        window._deviceflow.searching = True  # discovery still running

    def blocked_cli(window: Any) -> None:
        window._deviceflow.cli_blocked = True  # incompatible CLI

    cases: tuple[tuple[str, Any], ...] = (
        ("no device", lambda w: setattr(w, "device", None)),
        ("searching", searching),
        ("blocked CLI", blocked_cli),
        ("blocked choice", lambda w: setattr(w, "_selection_block_reason", "x")),
        ("running scan", lambda w: setattr(w, "_runner", object())),
    )
    for name, prepare in cases:
        window = Window()
        prepare(window)
        window._update_scan_enabled()  # type: ignore[misc]
        assert window._form.enabled is False, name


@_NEEDS_DESKTOP
def test_sigint_exits_with_130_and_saves_settings(tmp_path: Path) -> None:
    stderr_file = tmp_path / "gui-stderr.log"
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    # set -m: without job control, backgrounded jobs inherit SIGINT ignored
    # (POSIX) and the interrupt would never reach the GUI.
    script = (
        f"set -m; scanmole-gui 2>'{stderr_file}' & pid=$!; "
        "sleep 5; kill -INT $pid; wait $pid; echo EXIT:$?"
    )
    result = subprocess.run(
        ["dbus-run-session", "--", "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )

    assert "EXIT:130" in result.stdout
    assert "Traceback" not in stderr_file.read_text()
    # The SIGINT path must persist state like a normal window close does.
    assert (tmp_path / "config" / "scanmole" / "gui.json").is_file()


@_NEEDS_DESKTOP
def test_sigint_kills_a_hung_advisory_discovery_child(tmp_path: Path) -> None:
    # The orphan regression: a wedged device search must not survive the
    # GUI. The fake CLI answers the version probe, then hangs the device
    # listing; interrupting the GUI has to take the whole advisory child
    # group down with it.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # A per-run marker duration: a stale child of an earlier run must
    # never satisfy (or poison) this run's pgrep checks.
    marker = f"47{os.getpid() % 10000}.375"
    fake = fake_bin / "scanmole"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "scanmole 0.0.0"; exit 0; fi\n'
        f"exec sleep {marker}\n"
    )
    fake.chmod(0o755)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    # Bracket the final character: the regex still matches the real
    # child's argv but never the script's own command line, which embeds
    # this very pattern.
    probe = f"pgrep -f 'sleep {marker[:-1]}[{marker[-1]}]' >/dev/null"
    script = (
        "set -m; scanmole-gui & pid=$!; "
        f"for i in $(seq 1 100); do {probe} && break; sleep 0.1; done; "
        f"{probe}; echo PROBE:$?; "
        "kill -INT $pid; wait $pid; echo EXIT:$?; "
        f"for i in $(seq 1 50); do {probe} || break; sleep 0.1; done; "
        f"{probe}; echo ORPHAN:$?"
    )
    try:
        result = subprocess.run(
            ["dbus-run-session", "--", "bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
            check=False,
        )
    finally:
        subprocess.run(["pkill", "-f", f"sleep {marker}"], check=False)

    assert "PROBE:0" in result.stdout  # the hung advisory child was running
    assert "EXIT:130" in result.stdout
    assert "ORPHAN:1" in result.stdout  # and it did not survive the GUI


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_shutdown_after_a_close_never_persists_again() -> None:
    # The normal close already persisted against the live window; the
    # application shutdown signal then reaches the destroyed window via
    # the app's own reference, where get_width() reads 0. A second
    # persist there overwrote the just-saved geometry with zeros.
    from scanmole_gui.app import MainWindow

    class Runner:
        def __init__(self) -> None:
            self.shutdowns = 0

        def shutdown(self) -> None:
            self.shutdowns += 1

    class DeviceFlowDouble:
        def stop(self) -> None:
            pass

    class Window:
        _shutdown_now = MainWindow._shutdown_now

        def __init__(self) -> None:
            self.persisted = 0
            self._preview = _PreviewFlowDouble()
            self._released = True  # the close request already ran
            self._deviceflow = DeviceFlowDouble()
            self._runner: Runner | None = Runner()

        def _persist_ui_state(self) -> None:
            self.persisted += 1

    window = Window()
    window._shutdown_now()  # type: ignore[misc]

    assert window.persisted == 0  # the close-time snapshot stays untouched
    assert window._runner is not None and window._runner.shutdowns == 1
    assert window._preview.stopped is True  # torn down again, harmlessly


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_persist_skips_geometry_of_a_destroyed_window() -> None:
    # Defense in depth for any future path: a window reporting a
    # nonpositive size is gone or unrealized, and neither its size nor
    # its maximized flag is real state worth storing.
    from scanmole_gui.app import MainWindow

    class Window:
        _persist_ui_state = MainWindow._persist_ui_state

        def __init__(self, width: int) -> None:
            self._settings: dict[str, object] = {"window_width": 900}
            self.saved = 0
            self.width = width

        def is_maximized(self) -> bool:
            return False

        def get_width(self) -> int:
            return self.width

        def get_height(self) -> int:
            return 700 if self.width else 0

        def _save_settings(self) -> None:
            self.saved += 1

    dead = Window(width=0)
    dead._persist_ui_state()  # type: ignore[misc]
    assert dead._settings["window_width"] == 900  # zeros never overwrite
    assert "window_maximized" not in dead._settings

    live = Window(width=1050)
    live._persist_ui_state()  # type: ignore[misc]
    assert live._settings["window_width"] == 1050
    assert live.saved == 1


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_restored_window_size_heals_persisted_zeros() -> None:
    # Files written by the destroyed-window persist carry zeros; they
    # must restore the default instead of a zero-sized window.
    from scanmole_gui.app import DEFAULT_WINDOW_SIZE, restored_window_size

    assert restored_window_size({}) == DEFAULT_WINDOW_SIZE
    assert (
        restored_window_size({"window_width": 0, "window_height": 0})
        == DEFAULT_WINDOW_SIZE
    )
    assert (
        restored_window_size({"window_width": 900, "window_height": 0})
        == DEFAULT_WINDOW_SIZE
    )
    assert restored_window_size({"window_width": 900, "window_height": 700}) == (
        900,
        700,
    )


class _PresenceForm:
    """Records every widget write of the device-render path."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, object]] = []
        self.scan_enabled: bool | None = None

    def set_refresh_enabled(self, enabled: bool) -> None:
        pass  # idempotent re-enable; not a visible write

    def show_devices(self, names: list[str], index: int) -> None:
        self.writes.append(("devices", tuple(names)))

    def set_device_subtitle(self, text: str) -> None:
        self.writes.append(("subtitle", text))

    def set_device_tooltip(self, text: str) -> None:
        self.writes.append(("tooltip", text))

    def set_scan_enabled(self, enabled: bool) -> None:
        self.scan_enabled = enabled


def _listing_window(devices: list[dict[str, str]]) -> Any:
    """A window stub carrying only the device-listing render path.

    The decisions behind the outcome (quiet comparison, staleness, the
    poll chain) belong to the controller and are pinned in
    ``test_gui_deviceflow.py``; this stub renders decided outcomes.
    """
    from scanmole_gui.app import MainWindow

    class DeviceFlowDouble:
        searching = False
        cli_blocked = False

    class Window:
        _render_device_listing = MainWindow._render_device_listing
        _discovery_failure_text = MainWindow._discovery_failure_text
        _scan_allowed = MainWindow._scan_allowed
        _update_scan_enabled = MainWindow._update_scan_enabled

        def __init__(self) -> None:
            self._released = False
            self._runner = None
            self._selection_block_reason = None
            self._version_alert_shown = False
            self._settings: dict[str, object] = {}
            self._deviceflow = DeviceFlowDouble()
            self._devices = list(devices)
            self._form = _PresenceForm()
            self.bars: list[str] = []
            self.logs: list[str] = []
            self.alerts: list[str] = []

        def _selected_device(self) -> str | None:
            return self._devices[0]["device"] if self._devices else None

        def _set_result_bar(self, state: str, title: str, detail: str = "") -> None:
            self.bars.append(title)

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _alert(self, heading: str, body: str) -> None:
            self.alerts.append(heading)

    return Window()


_IX100 = {"device": "fujitsu:ScanSnap iX100:X", "vendor": "FUJITSU", "model": "iX100"}


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_an_unchanged_presence_outcome_touches_nothing() -> None:
    from scanmole_gui.deviceflow import ListingOutcome

    window = _listing_window([_IX100])

    window._render_device_listing(
        ListingOutcome(devices=[dict(_IX100)], unchanged=True, poll_scheduled=True)
    )

    assert window._form.writes == []  # no model rebuild, no subtitle
    assert window.bars == []  # no "Found 1 scanner." repaint


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_changed_listing_still_applies_fully() -> None:
    from scanmole_gui.deviceflow import ListingOutcome

    window = _listing_window([_IX100])
    second = {"device": "epsonds:net:host", "vendor": "EPSON", "model": "DS"}

    window._render_device_listing(
        ListingOutcome(
            devices=[dict(_IX100), second],
            prefer=str(_IX100["device"]),
        )
    )

    assert ("devices", ("FUJITSU iX100", "EPSON DS")) in [
        (kind, value) for kind, value in window._form.writes
    ]
    assert len(window._devices) == 2
    assert any("Found 2 scanners." in title for title in window.bars)


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_vanished_selected_device_is_reported_and_gates_start() -> None:
    from scanmole_gui.deviceflow import ListingOutcome

    window = _listing_window([_IX100])

    window._render_device_listing(ListingOutcome(devices=[], vanished=True))

    subtitles = [value for kind, value in window._form.writes if kind == "subtitle"]
    assert subtitles and "disconnected" in str(subtitles[-1])
    assert any("disappeared" in line for line in window.logs)
    assert window._form.scan_enabled is False  # no device: Start gated


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_the_compatibility_alert_appears_exactly_once() -> None:
    from scanmole_gui.deviceflow import DiscoveryFailure, ListingOutcome

    window = _listing_window([])
    outcome = ListingOutcome(
        devices=[],
        cli_version="0.9.0",
        needed=">= 2, < 3",
        failure=DiscoveryFailure.INCOMPATIBLE_CLI,
    )

    window._render_device_listing(outcome)
    window._render_device_listing(outcome)

    assert window.alerts == ["Incompatible scanmole CLI"]  # one-time alert
    subtitles = [value for kind, value in window._form.writes if kind == "subtitle"]
    assert subtitles and "Incompatible scanmole CLI" in str(subtitles[-1])
