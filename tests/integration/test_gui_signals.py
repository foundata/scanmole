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
