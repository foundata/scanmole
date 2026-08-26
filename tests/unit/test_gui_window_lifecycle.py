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

            def pause_for_scan(*_args: object) -> bool:
                self.steps.append("pause")
                return True

            def resume_after_scan(*_args: object) -> None:
                self.steps.append("resume")

            self._deviceflow = type(
                "D",
                (),
                {
                    "pause_for_scan": pause_for_scan,
                    "resume_after_scan": resume_after_scan,
                },
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

        def _update_scan_enabled(self) -> None:
            self.steps.append("enablement")

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
def test_every_reset_restores_the_window_size_not_only_the_first() -> None:
    # GTK resizes a mapped window only when the default-size property
    # actually changes, and that property does not follow a resize the
    # user performed themselves. So after one reset it already holds the
    # default, and re-setting the same value queues nothing: the window
    # would keep whatever size it was dragged to for the rest of the
    # session. The stub below models exactly those two GTK rules.
    from scanmole_gui.app import DEFAULT_WINDOW_SIZE, MainWindow

    class Window:
        _restore_default_geometry = MainWindow._restore_default_geometry

        def __init__(self) -> None:
            self.default: tuple[int, int] = (1200, 900)
            self.size: tuple[int, int] = (1200, 900)

        def set_default_size(self, width: int, height: int) -> None:
            if (width, height) == self.default:
                return  # no property change, so GTK queues no resize
            self.default = (width, height)
            if width > 0 and height > 0:
                self.size = (width, height)

        def drag(self, width: int, height: int) -> None:
            """A user resize: the surface moves, the property does not."""
            self.size = (width, height)

    window: Any = Window()

    window.drag(1400, 1000)
    window._restore_default_geometry()
    assert window.size == DEFAULT_WINDOW_SIZE  # the reset that always worked

    window.drag(1400, 1000)
    window._restore_default_geometry()
    assert window.size == DEFAULT_WINDOW_SIZE  # and every one after it

    # Idempotent with no drag in between: still the default, never the
    # natural size the property is cleared to on the way there.
    window._restore_default_geometry()
    assert window.size == DEFAULT_WINDOW_SIZE


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
    # already paused device coordination (the controller pins its
    # internal stop-cancel-reset order in test_gui_deviceflow). A
    # raising start must not adopt a runner, and it shows the usual
    # start alert. Coordination then resumes right here: the resume
    # negotiates before the enablement pass that follows, so nothing can
    # arm sensor polling from the evidence the takeover invalidated.
    def refuse(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise OSError("no such executable")

    window = _scan_window(tmp_path, monkeypatch, start=refuse)

    window._on_scan_clicked()

    assert window.steps == ["pause", "resume", "enablement"]
    assert window._runner is None  # nothing to block a later attempt
    assert [heading for heading, _b in window.alerts] == ["Could Not Start scanmole"]


class _FakeGLib:
    """Deterministic GLib stand-in: sources fire only when told to."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.timeouts: dict[int, tuple[int, Any]] = {}
        self.idles: list[Any] = []
        self._next = 1

    def timeout_add(self, ms: int, callback: Any) -> int:
        self.timeouts[self._next] = (ms, callback)
        self._next += 1
        return self._next - 1

    def timeout_add_seconds(self, seconds: int, callback: Any) -> int:
        return self.timeout_add(seconds * 1000, callback)

    def idle_add(self, callback: Any, *args: object) -> int:
        self.idles.append((callback, args))
        return 0

    def source_remove(self, source: int) -> None:
        self.timeouts.pop(source, None)


def _capability(kind: Any = "bool", **kw: Any) -> Any:
    from scanmole.options import Capability

    return Capability(kind=kind, **kw)


def _snapshot() -> dict[str, Any]:
    """A duplex feeder with a scan button and a paper level."""
    return {
        "source": _capability(
            "enum", choices=["ADF Front", "ADF Duplex"], current="ADF Duplex"
        ),
        "mode": _capability("enum", choices=["Lineart", "Gray", "Color"]),
        "scan": _capability(current="no"),
        "page-loaded": _capability(current="no"),
    }


def _takeover_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start: Callable[..., None],
) -> Any:
    """A window stub wired to a real controller with held workers.

    Probe workers are never run: each spawn is recorded and the test
    feeds the controller's completion handler directly, so every step
    is deterministic.
    """
    from scanmole_gui import app as app_module
    from scanmole_gui import deviceflow as deviceflow_module
    from scanmole_gui.app import MainWindow
    from scanmole_gui.deviceflow import DeviceFlow

    glib = _FakeGLib()
    monkeypatch.setattr(deviceflow_module, "GLib", glib)
    monkeypatch.setattr(
        app_module, "ScanRunner", lambda **_kw: type("R", (), {"start": start})()
    )
    monkeypatch.setattr(app_module, "request_argv", lambda _r, _c: ["scanmole"])
    monkeypatch.setattr(app_module, "SessionState", lambda **_kw: object())

    class Window:
        _on_scan_clicked = MainWindow._on_scan_clicked
        _device_context = MainWindow._device_context
        _render_capability_update = MainWindow._render_capability_update
        _update_scan_enabled = MainWindow._update_scan_enabled
        _scan_allowed = MainWindow._scan_allowed
        _update_selection_block = MainWindow._update_selection_block
        _sensor_prefs = MainWindow._sensor_prefs
        _sensor_trigger_allowed = MainWindow._sensor_trigger_allowed
        _on_sensor_trigger = MainWindow._on_sensor_trigger
        _window_suspended = MainWindow._window_suspended

        def __init__(self) -> None:
            self.glib = glib
            self._runner = None
            self._released = False
            self._closing = False
            self._selection_block_reason: str | None = None
            self._settings: dict[str, object] = {"hardware_button": "same"}
            self._scanmole = "scanmole"
            self.device = "epsonds:net:host"
            self.spawned: list[Any] = []
            self.alerts: list[tuple[str, str]] = []
            self.logs: list[str] = []
            self.folder = tmp_path / "out"

            window = self

            class Form:
                @staticmethod
                def folder() -> str:
                    return str(window.folder)

                @staticmethod
                def source_value() -> str:
                    return "adf-duplex"

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
                def selection_blocked_reason() -> str | None:
                    return None

                set_running = staticmethod(lambda _running: None)
                set_scan_enabled = staticmethod(lambda _enabled: None)
                set_source_availability = staticmethod(lambda _blocked: None)
                set_mode_availability = staticmethod(lambda _blocked: None)
                select_source = staticmethod(lambda _value: None)
                refresh_document_hints = staticmethod(lambda: None)

            self._form = Form()
            self._deviceflow = DeviceFlow(
                scanmole="scanmole",
                context=self._device_context,  # type: ignore[misc]
                on_searching=lambda: None,
                on_listing=lambda _outcome: None,
                on_capabilities=self._render_capability_update,  # type: ignore[misc]
                on_trigger=self._on_sensor_trigger,  # type: ignore[misc]
                on_log=self._append_log,
            )

            class Advisory:
                generation = 0

                @staticmethod
                def spawn_worker(_target: Any, *args: object) -> None:
                    # (token, request, generation) of a probe worker; the
                    # test feeds the completion handler itself.
                    window.spawned.append(args)

                @staticmethod
                def cancel_pending(*_a: object, **_kw: object) -> bool:
                    Advisory.generation += 1
                    return True

                @staticmethod
                def adopter(_generation: int) -> Any:
                    return lambda _process: None

            self._deviceflow._advisory = Advisory()  # type: ignore[assignment]

        def _selected_device(self) -> str | None:
            return self.device

        def get_visible(self) -> bool:
            return True

        def _alert(self, heading: str, body: str) -> None:
            self.alerts.append((heading, body))

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _set_result_bar(self, *_args: object, **_kw: object) -> None:
            pass

        def _save_settings(self) -> None:
            pass

        # Runner callbacks: the stub runner never invokes them.
        _schedule = staticmethod(lambda _cb: None)
        _after_seconds = staticmethod(lambda _s, _cb: None)
        _on_stdout_line = staticmethod(lambda *_a: None)
        _on_stderr_line = staticmethod(lambda *_a: None)
        _on_process_exit = staticmethod(lambda *_a: None)
        _on_kill_escalated = staticmethod(lambda *_a: None)

    window: Any = Window()
    return window


def _complete_next_probe(window: Any) -> None:
    """Feed the snapshot back for the most recently spawned probe."""
    token, request, generation = window.spawned.pop()
    window._deviceflow._probe_done(token, request, _snapshot(), generation)


def _settle_negotiation(window: Any) -> None:
    """Complete the bare probe and its source-applied follow-up."""
    _complete_next_probe(window)  # bare: derives source availability
    _complete_next_probe(window)  # refinement under the selected source


@_NEEDS_GI
def test_a_failed_start_negotiates_again_without_waiting_for_a_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The takeover reset the capability flow for a scan that never
    # started. Waiting for the quiet presence poll would not repair
    # that: an unchanged listing skips negotiation, so the flow's base
    # snapshot would stay empty for good. The failed start itself must
    # start a fresh bare negotiation for the selected device.
    def refuse(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise OSError("no such executable")

    window = _takeover_window(tmp_path, monkeypatch, start=refuse)
    window._deviceflow.device_changed()
    _settle_negotiation(window)
    assert window._deviceflow.last_caps is not None

    window._on_scan_clicked()

    # A fresh bare probe of the current selection is already on its way;
    # no timer is armed for it, so no poll interval is involved.
    assert window._runner is None
    assert window._deviceflow._flow.probe_active is True
    assert len(window.spawned) == 1
    _token, request, _generation = window.spawned[0]
    assert request.device == window.device
    assert request.settings == ()  # bare first, exactly like startup
    # The existing failure surface is unchanged.
    assert [heading for heading, _b in window.alerts] == ["Could Not Start scanmole"]
    assert any(
        line.startswith("[gui] failed to start scanmole:") for line in window.logs
    )


@_NEEDS_GI
def test_no_sensor_poll_restarts_from_retained_caps_after_a_failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flow reset cleared the base snapshot but kept ``last_caps``,
    # so sensor settings for the selected source are not derivable: a
    # poll armed from the retained caps would read the backend's default
    # source. Until the fresh probe is accepted, every poll attempt (an
    # unchanged quiet presence result ends in one) must stay unarmed.
    def refuse(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise OSError("no such executable")

    window = _takeover_window(tmp_path, monkeypatch, start=refuse)
    window._deviceflow.device_changed()
    _settle_negotiation(window)
    assert window.glib.timeouts  # idle polling armed before the click

    window._on_scan_clicked()

    flow = window._deviceflow
    assert flow._flow.sensor_settings(window.device, "adf-duplex") == ()
    assert window.glib.timeouts == {}  # the takeover disarmed the poller
    flow._schedule_sensor_poll()  # what an unchanged quiet poll attempts
    assert window.glib.timeouts == {}  # still gated on the fresh probe


@_NEEDS_GI
def test_sensor_polling_resumes_after_the_fresh_probe_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once the recovery negotiation's source-applied probe lands, the
    # ordinary rules apply again: polling is wanted and reads under the
    # selected source, and the accepted live probe is the baseline.
    def refuse(_self: object, _argv: list[str], _cwd: Path) -> None:
        raise OSError("no such executable")

    window = _takeover_window(tmp_path, monkeypatch, start=refuse)
    window._deviceflow.device_changed()
    _settle_negotiation(window)
    window._on_scan_clicked()

    _settle_negotiation(window)

    flow = window._deviceflow
    assert flow._flow.probe_active is False
    assert flow._flow.sensor_settings(window.device, "adf-duplex") == (
        ("--source", "ADF Duplex"),
    )
    assert len(window.glib.timeouts) == 1  # the idle poller is armed again
    assert window._runner is None
