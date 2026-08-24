"""Tests for MainWindow's own lifecycle commands (no GTK loop, no hardware).

Pressing Start, answering a close that would discard captured pages and
asking for a restart are the window's own decisions: they belong to no
controller it delegates to, so they are pinned here rather than beside
the preview, sensor or advisory machinery they happen to sit next to.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
]

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)


def _scan_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, start: Callable[..., None]
) -> Any:
    """A window stub carrying only the scan-start folder handling."""
    from scanmole_gui.app import MainWindow

    class Window:
        _on_scan_clicked = MainWindow._on_scan_clicked

        def __init__(self) -> None:
            self.steps: list[str] = []
            self._runner = None
            self._searching = False

            def cancel_pending(*_args: object, **_kw: object) -> bool:
                self.steps.append("cancel")
                return True

            self._advisory = type("A", (), {"cancel_pending": cancel_pending})()
            self._flow = type(
                "F", (), {"reset": lambda *_a: self.steps.append("flow-reset")}
            )()
            self._scanmole = "scanmole"
            self.alerts: list[tuple[str, str]] = []
            self.logs: list[str] = []
            self.folder = tmp_path / "out"

            window = self

            class Form:
                def folder(self) -> str:
                    return str(window.folder)

                @staticmethod
                def sheet_flow_value() -> str:
                    return "stack"

                @staticmethod
                def scan_request(device: object, folder: Path, **_kw: object) -> Any:
                    return type(
                        "Request",
                        (),
                        {"drop_blanks": True, "output": str(folder / "out.pdf")},
                    )()

                @staticmethod
                def set_running(_running: bool) -> None:
                    pass

            self._form = Form()

        def _stop_sensor_polling(self) -> None:
            self.steps.append("stop-sensors")

        def _save_settings(self) -> None:
            pass

        def _selected_device(self) -> str | None:
            return "test:0"

        def _alert(self, heading: str, body: str) -> None:
            self.alerts.append((heading, body))

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _set_result_bar(self, *_args: object, **_kw: object) -> None:
            pass

        # Runner callbacks: the stub runner never invokes them.
        _schedule = staticmethod(lambda _cb: None)
        _after_seconds = staticmethod(lambda _s, _cb: None)
        _on_stdout_line = staticmethod(lambda *_a: None)
        _on_stderr_line = staticmethod(lambda *_a: None)
        _on_process_exit = staticmethod(lambda *_a: None)
        _on_kill_escalated = staticmethod(lambda *_a: None)

    from scanmole_gui import app as app_module

    monkeypatch.setattr(
        app_module, "ScanRunner", lambda **_kw: type("R", (), {"start": start})()
    )
    monkeypatch.setattr(app_module, "request_argv", lambda _r, _c: ["scanmole"])
    monkeypatch.setattr(app_module, "SessionState", lambda **_kw: object())
    return Window()


@_NEEDS_GI
def test_a_missing_output_folder_is_created_before_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []
    window = _scan_window(
        tmp_path, monkeypatch, start=lambda _self, _argv, cwd: started.append(cwd)
    )
    assert not window.folder.exists()

    window._on_scan_clicked()

    assert window.folder.is_dir()  # created as before
    assert started == [window.folder]
    assert window.alerts == []


@_NEEDS_GI
def test_an_uncreatable_output_folder_still_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _scan_window(tmp_path, monkeypatch, start=lambda *_a: None)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    window.folder = blocker / "out"

    window._on_scan_clicked()

    assert [heading for heading, _body in window.alerts] == [
        "Cannot Create Output Folder"
    ]


@_NEEDS_GI
def test_a_folder_deleted_before_the_spawn_is_not_an_installation_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The folder existed a moment ago, so pointing the user at the CLI
    # installation would send them after entirely the wrong thing.
    def vanish(_self: object, _argv: list[str], cwd: Path) -> None:
        cwd.rmdir()
        raise FileNotFoundError(2, "No such file or directory", str(cwd))

    window = _scan_window(tmp_path, monkeypatch, start=vanish)

    window._on_scan_clicked()

    headings = [heading for heading, _body in window.alerts]
    assert headings == ["Cannot Create Output Folder"]
    assert not any("Install the scanmole CLI" in body for _h, body in window.alerts)


@_NEEDS_GI
def test_a_missing_cli_still_points_at_the_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_executable(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise FileNotFoundError(2, "No such file or directory", "scanmole")

    window = _scan_window(tmp_path, monkeypatch, start=no_executable)

    window._on_scan_clicked()

    assert [heading for heading, _body in window.alerts] == ["Could Not Start scanmole"]
    assert any("Install the scanmole CLI" in body for _h, body in window.alerts)


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_closing_asks_only_when_captured_pages_would_be_lost() -> None:
    # The prompt exists to stop a discard, so it must not appear for a
    # close that loses nothing: an idle window, or a run that has not
    # delivered a page yet.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.session import SessionState

    class Runner:
        def __init__(self) -> None:
            self.running = True

        def is_running(self) -> bool:
            return self.running

    class Window:
        _close_discards_pages = MainWindow._close_discards_pages

        def __init__(self) -> None:
            self._runner: Any = Runner()
            self._session = SessionState(drop_blanks=True, pages=2)
            self._closing = False
            self._close_confirmed = False

    window: Any = Window()
    assert window._close_discards_pages() is True

    window._session = SessionState(drop_blanks=True, pages=0)  # nothing captured yet
    assert window._close_discards_pages() is False
    window._session = SessionState(drop_blanks=True, pages=2)

    window._runner.running = False  # the scan already finished
    assert window._close_discards_pages() is False
    window._runner.running = True

    window._runner = None  # idle window
    assert window._close_discards_pages() is False
    window._runner = Runner()

    window._closing = True  # the close is already under way
    assert window._close_discards_pages() is False
    window._closing = False

    window._close_confirmed = True  # answered once; never asked twice
    assert window._close_discards_pages() is False


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_confirmed_discard_closes_and_echoes_the_log_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The engine keeps preserving after the window is hidden, and its
    # recovery command is the only way back to those pages; the log pane
    # it would land in is gone.
    from scanmole_gui.app import MainWindow

    class Window:
        _on_close_confirm_response = MainWindow._on_close_confirm_response
        _append_log = MainWindow._append_log

        def __init__(self) -> None:
            self._close_confirmed = False
            self._echo_log = False
            self.closed = 0

            class Log:
                def __init__(self) -> None:
                    self.lines: list[str] = []

                def append(self, text: str) -> None:
                    self.lines.append(text)

            self._log = Log()

        def close(self) -> None:
            self.closed += 1

    window: Any = Window()
    window._on_close_confirm_response(None, "keep")
    assert window.closed == 0 and window._echo_log is False

    window._append_log("[gui] before the close")
    assert capsys.readouterr().err == ""

    window._on_close_confirm_response(None, "close")
    assert window.closed == 1
    window._append_log("kept in /tmp/scanmole-x (recover with: ...)")

    assert "recover with" in capsys.readouterr().err
    assert window._log.lines[-1].startswith("kept in")  # still in the pane


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_restart_reaches_the_application_only_through_a_real_close() -> None:
    # Restart is a request to close and come back. While the close is
    # still open to refusal the intent stays in the window, or a declined
    # restart would re-execute on some later, unrelated quit.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.session import SessionState

    class App:
        def __init__(self) -> None:
            self.restart_requested = False

    class Runner:
        def is_running(self) -> bool:
            return True

    class Window:
        _on_restart_clicked = MainWindow._on_restart_clicked
        _close_discards_pages = MainWindow._close_discards_pages
        _on_close_confirm_response = MainWindow._on_close_confirm_response
        _append_log = MainWindow._append_log

        def __init__(self, pages: int) -> None:
            self._app = App()
            self._settings_dialog = None
            self._runner: Any = Runner()
            self._session = SessionState(drop_blanks=True, pages=pages)
            self._closing = False
            self._close_confirmed = False
            self._restart_pending = False
            self._echo_log = False
            self._log = type("Log", (), {"append": lambda self, text: None})()
            self.prompts = 0

        def get_application(self) -> App:
            return self._app

        def restart_reached_the_app(self) -> bool:
            return self._app.restart_requested

        def close(self) -> None:
            # Stands in for GTK dispatching close-request, whose real
            # handler transfers the intent once the close goes through.
            window: Any = self
            if window._close_discards_pages():
                self.prompts += 1
                return
            if self._restart_pending:
                self._app.restart_requested = True

    plain: Any = Window(pages=0)  # nothing captured: no question asked
    plain._on_restart_clicked()
    assert plain.prompts == 0
    assert plain.restart_reached_the_app() is True

    confirmed: Any = Window(pages=3)
    confirmed._on_restart_clicked()
    assert confirmed.prompts == 1  # inhibited by the discard prompt
    assert confirmed.restart_reached_the_app() is False
    confirmed._on_close_confirm_response(None, "close")
    assert confirmed.restart_reached_the_app() is True

    # "Keep Scanning" leaves nothing behind for a later ordinary quit.
    # Dismissing the dialog arrives here as the same response, because
    # ``_confirm_close`` registers "keep" as its close response.
    declined: Any = Window(pages=3)
    declined._on_restart_clicked()
    declined._on_close_confirm_response(None, "keep")
    assert declined._restart_pending is False
    declined._session = SessionState(drop_blanks=True, pages=0)
    declined.close()  # an ordinary close, much later
    assert declined.restart_reached_the_app() is False


@_NEEDS_GI
def test_a_failed_start_completes_device_takeover_without_adopting_a_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The takeover runs before the spawn, so a start that raises has
    # already stopped the poller, cancelled advisory work and reset the
    # flow, in that order. A raising start must not adopt a runner or
    # leave the searching latch set, and it shows the usual start alert.
    def refuse(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise OSError("no such executable")

    window = _scan_window(tmp_path, monkeypatch, start=refuse)

    window._on_scan_clicked()

    assert window.steps == ["stop-sensors", "cancel", "flow-reset"]
    assert window._runner is None  # nothing to block a later attempt
    assert window._searching is False  # the search latch is not left set
    assert [heading for heading, _b in window.alerts] == ["Could Not Start scanmole"]
