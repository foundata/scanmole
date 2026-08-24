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
            self._runner = None
            self._searching = False
            self._advisory = type("A", (), {"cancel_pending": lambda *_a: True})()
            self._flow = type("F", (), {"reset": lambda *_a: None})()
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
            pass

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
