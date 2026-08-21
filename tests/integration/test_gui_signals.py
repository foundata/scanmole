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

    from scanmole_gui.advisory import AdvisoryCommands

    class Window:
        _shutdown_now = MainWindow._shutdown_now

        def __init__(self) -> None:
            self.persisted = 0
            self._released = False
            self._advisory = AdvisoryCommands()
            self._runner: Runner | None = Runner()

        def _persist_ui_state(self) -> None:
            self.persisted += 1

        def _stop_sensor_polling(self) -> None:
            pass

    window = Window()
    window._shutdown_now()  # type: ignore[misc]
    assert window.persisted == 1
    assert window._runner is not None and window._runner.shutdowns == 1

    idle = Window()
    idle._runner = None
    idle._shutdown_now()  # type: ignore[misc]
    assert idle.persisted == 1  # state persists even without a scan


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

    class Window:
        _update_scan_enabled = MainWindow._update_scan_enabled
        _scan_allowed = MainWindow._scan_allowed

        def __init__(self) -> None:
            self._form = Form()
            self._runner = None
            self._cli_blocked = False
            self._selection_block_reason: str | None = None
            self._searching = False
            self.device: str | None = "sane:0"

        def _selected_device(self) -> str | None:
            return self.device

        def _schedule_sensor_poll(self) -> None:
            pass

    window = Window()
    window._update_scan_enabled()  # type: ignore[misc]
    assert window._form.enabled is True  # idle, device selected

    for attribute, value in (
        ("device", None),  # nothing to scan with
        ("_searching", True),  # discovery still running
        ("_cli_blocked", True),  # incompatible CLI
        ("_selection_block_reason", "no duplex"),  # blocked saved choice
        ("_runner", object()),  # a scan already runs
    ):
        window = Window()
        setattr(window, attribute, value)
        window._update_scan_enabled()  # type: ignore[misc]
        assert window._form.enabled is False, attribute


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
    from scanmole_gui.advisory import AdvisoryCommands
    from scanmole_gui.app import MainWindow

    class Runner:
        def __init__(self) -> None:
            self.shutdowns = 0

        def shutdown(self) -> None:
            self.shutdowns += 1

    class Window:
        _shutdown_now = MainWindow._shutdown_now

        def __init__(self) -> None:
            self.persisted = 0
            self._released = True  # the close request already ran
            self._advisory = AdvisoryCommands()
            self._runner: Runner | None = Runner()

        def _persist_ui_state(self) -> None:
            self.persisted += 1

        def _stop_sensor_polling(self) -> None:
            pass

    window = Window()
    window._shutdown_now()  # type: ignore[misc]

    assert window.persisted == 0  # the close-time snapshot stays untouched
    assert window._runner is not None and window._runner.shutdowns == 1


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


class _FakeGLib:
    """Records timeout scheduling; sources fire only when told to."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.timeouts: list[tuple[int, object]] = []
        self.removed: list[int] = []
        self._next = 1

    def timeout_add_seconds(self, seconds: int, callback: object) -> int:
        self.timeouts.append((seconds, callback))
        self._next += 1
        return self._next - 1

    def source_remove(self, source: int) -> None:
        self.removed.append(source)


class _PresenceForm:
    """Records every widget write of the device-apply path."""

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


def _presence_window(
    monkeypatch: pytest.MonkeyPatch, devices: list[dict[str, str]]
) -> Any:
    from scanmole_gui.advisory import AdvisoryCommands
    from scanmole_gui.app import MainWindow

    fake_glib = _FakeGLib()
    monkeypatch.setattr("scanmole_gui.app.GLib", fake_glib)

    class Window:
        _apply_devices = MainWindow._apply_devices
        _poll_devices = MainWindow._poll_devices
        _scan_allowed = MainWindow._scan_allowed
        _update_scan_enabled = MainWindow._update_scan_enabled

        def __init__(self) -> None:
            self.glib = fake_glib
            self._released = False
            self._advisory = AdvisoryCommands()
            self._searching = True
            self._runner = None
            self._cli_blocked = False
            self._version_alert_shown = False
            self._selection_block_reason = None
            self._devices = list(devices)
            self._device_poll_id: int | None = None
            self._form = _PresenceForm()
            self.bars: list[str] = []
            self.logs: list[str] = []
            self.negotiations = 0
            self.refreshes: list[bool] = []

        def _selected_device(self) -> str | None:
            return self._devices[0]["device"] if self._devices else None

        def _set_result_bar(self, state: str, title: str, detail: str = "") -> None:
            self.bars.append(title)

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _start_negotiation(self) -> None:
            self.negotiations += 1

        def _schedule_sensor_poll(self) -> None:
            pass

        def _refresh_devices(self, *, quiet: bool = False) -> None:
            self.refreshes.append(quiet)

        def is_suspended(self) -> bool:
            return False

    return Window()


_IX100 = {"device": "fujitsu:ScanSnap iX100:X", "vendor": "FUJITSU", "model": "iX100"}


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_quiet_unchanged_presence_check_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _presence_window(monkeypatch, [_IX100])

    window._apply_devices([dict(_IX100)], "", _IX100["device"], 0, True)

    assert window._form.writes == []  # no model rebuild, no subtitle
    assert window.bars == []  # no "Found 1 scanner." repaint
    assert window.negotiations == 0  # no probe churn
    assert window._searching is False
    # The next presence check is armed at the slow cadence.
    assert [seconds for seconds, _cb in window.glib.timeouts] == [45]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_changed_list_still_applies_fully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _presence_window(monkeypatch, [_IX100])
    second = {"device": "epsonds:net:host", "vendor": "EPSON", "model": "DS"}

    window._apply_devices([dict(_IX100), second], "", _IX100["device"], 0, True)

    assert ("devices", ("FUJITSU iX100", "EPSON DS")) in [
        (kind, value) for kind, value in window._form.writes
    ]
    assert window.negotiations == 1
    assert len(window._devices) == 2


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_vanished_selected_device_is_reported_and_gates_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _presence_window(monkeypatch, [_IX100])

    window._apply_devices([], "", _IX100["device"], 0, True)

    subtitles = [value for kind, value in window._form.writes if kind == "subtitle"]
    assert subtitles and "disconnected" in str(subtitles[-1])
    assert any("disappeared" in line for line in window.logs)
    assert window._form.scan_enabled is False  # no device: Start gated
    # An empty list polls at the fast pickup cadence again.
    assert [seconds for seconds, _cb in window.glib.timeouts] == [15]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_the_poll_keeps_running_while_a_device_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _presence_window(monkeypatch, [_IX100])
    window._searching = False

    window._poll_devices()

    assert window.refreshes == [True]  # quiet presence check, not a UI search

    blocked = _presence_window(monkeypatch, [_IX100])
    blocked._searching = False
    blocked._cli_blocked = True
    blocked._poll_devices()
    assert blocked.refreshes == []  # a blocked CLI stops the polling
