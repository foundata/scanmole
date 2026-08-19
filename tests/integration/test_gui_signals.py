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
def test_sole_available_source_is_adopted_and_preference_kept() -> None:
    # A scanner offering exactly one source (the ScanSnap iX100: ADF Front
    # alone) must not leave Start disabled behind a blocked saved choice;
    # the sole source is adopted, the stored preference survives, and a
    # capable device gets it back. With a real choice left, nothing moves.
    from scanmole_gui.app import MainWindow

    class Row:
        def __init__(self, value: str) -> None:
            self._value = value
            self.history: list[str] = []

        def value(self) -> str:
            return self._value

        def select(self, value: str) -> None:
            self._value = value
            self.history.append(value)

    class Window:
        _reconcile_source_choice = MainWindow._reconcile_source_choice

        def __init__(self, current: str, preferred: str) -> None:
            self._source_row = Row(current)
            self._preferred_source = preferred
            self._reconciling_source = False
            self.log: list[str] = []

        def _append_log(self, text: str) -> None:
            self.log.append(text)

    ix100 = {"flatbed": "x", "adf-duplex": "x", "adf-back": "x"}

    window = Window(current="adf-duplex", preferred="adf-duplex")
    window._reconcile_source_choice(ix100)  # type: ignore[misc]
    assert window._source_row.value() == "adf"  # the sole source, adopted
    assert window._preferred_source == "adf-duplex"  # preference untouched
    assert any("only source" in line for line in window.log)

    # A duplex-capable device again: the stored preference comes back.
    window._reconcile_source_choice({})  # type: ignore[misc]
    assert window._source_row.value() == "adf-duplex"  # preference restored

    choice_left = Window(current="adf-back", preferred="adf-back")
    choice_left._reconcile_source_choice({"flatbed": "x", "adf-back": "x"})  # type: ignore[misc]
    assert choice_left._source_row.value() == "adf-back"  # no silent change
    assert choice_left._source_row.history == []


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # gi's own import noise
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_source_change_needs_the_selected_devices_own_snapshot() -> None:
    # While device B's bare probe is still queued behind A's, a source
    # change must not use A's retained snapshot to request a source-applied
    # probe for B; once B's own bare snapshot is applied, it may.
    from scanmole.options import Capability
    from scanmole_gui.app import MainWindow

    class Row:
        def __init__(self, value: str) -> None:
            self._value = value

        def value(self) -> str:
            return self._value

    class Window:
        _on_source_changed = MainWindow._on_source_changed

        def __init__(self, base_device: str) -> None:
            self._reconciling_source = False
            self._preferred_source = "adf-duplex"
            self._source_row = Row("adf-duplex")
            self._runner = None
            self._base_snapshot = {
                "source": Capability(kind="enum", choices=["ADF Duplex"])
            }
            self._base_snapshot_device = base_device
            self.launched: list[object] = []

        def _update_selection_block(self) -> None:
            pass

        def _selected_device(self) -> str:
            return "dev-b"

        def _launch_probe(self, request: object) -> None:
            self.launched.append(request)

    foreign = Window(base_device="dev-a")
    foreign._on_source_changed()  # type: ignore[misc]
    assert foreign.launched == []  # A's snapshot must never assess B

    own = Window(base_device="dev-b")
    own._on_source_changed()  # type: ignore[misc]
    assert [getattr(request, "settings", None) for request in own.launched] == [
        (("--source", "ADF Duplex"),)
    ]


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

    class Window:
        _shutdown_now = MainWindow._shutdown_now

        def __init__(self) -> None:
            self.persisted = 0
            self._runner: Runner | None = Runner()

        def _persist_ui_state(self) -> None:
            self.persisted += 1

    window = Window()
    window._shutdown_now()  # type: ignore[misc]
    assert window.persisted == 1
    assert window._runner is not None and window._runner.shutdowns == 1

    idle = Window()
    idle._runner = None
    idle._shutdown_now()  # type: ignore[misc]
    assert idle.persisted == 1  # state persists even without a scan


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
