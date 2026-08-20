"""Regression tests for the status views (PyGObject plus display)."""

from __future__ import annotations

import importlib.util
import os
from typing import Any

import pytest

pytestmark = [
    pytest.mark.skipif(
        importlib.util.find_spec("gi") is None
        or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="needs PyGObject and a display",
    ),
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


def _init_adw() -> Any:
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    return Adw


def test_log_view_appends_normalized_lines_and_copies() -> None:
    _init_adw()
    from scanmole_gui.status import LogView

    log = LogView()
    log.append("first line\n")
    log.append("second line")

    start, end = log._buffer.get_bounds()
    assert log._buffer.get_text(start, end, True) == "first line\nsecond line\n"
    log._on_copy()  # exercises the clipboard path without a paste target


def test_result_bar_states_and_actions() -> None:
    _init_adw()
    from scanmole_gui.status import ResultBar

    clicks: list[str] = []
    bar = ResultBar(
        on_show=lambda: clicks.append("show"), on_open=lambda: clicks.append("open")
    )

    bar.set_state("running", "Scanning")
    assert bar._spinner.get_visible() is True
    assert bar._icon.get_visible() is False
    assert bar._detail.get_visible() is False
    assert bar._show_btn.get_visible() is False

    bar.set_state("success", "2 pages saved", "out.pdf", actions=True)
    assert bar._spinner.get_visible() is False
    assert bar._icon.get_visible() is True
    assert bar._detail.get_visible() is True
    assert bar._title.get_text() == "2 pages saved"
    assert bar._detail.get_text() == "out.pdf"
    assert bar._show_btn.get_visible() is True and bar._open_btn.get_visible() is True
    bar._show_btn.emit("clicked")
    bar._open_btn.emit("clicked")
    assert clicks == ["show", "open"]

    bar.set_state("error", "Scan Failed")
    assert bar._icon.get_visible() is True
    assert bar._show_btn.get_visible() is False

    bar.set_state("idle", "Ready.")
    assert bar._icon.get_visible() is False


def test_render_session_update_texts() -> None:
    _init_adw()
    from scanmole_gui.session import SessionState, Update
    from scanmole_gui.status import render_session_update

    bars: list[str] = []
    logs: list[str] = []

    state = SessionState(drop_blanks=True, pages=3, blanks=1, total=5, kept=4)
    for update, expected in (
        (Update.STARTED, "Scanning…"),
        (Update.PAGE, "Page 3 scanned (1 blank skipped)…"),
        (
            Update.SCAN_DONE,
            "Scan finished — keeping 4 of 5 pages…",
        ),
        (Update.OCR_STARTED, "Running OCR…"),
    ):
        bars.clear()
        render_session_update(state, update, bars.append, logs.append)
        assert bars == [expected], update

    errored = SessionState(drop_blanks=True, error_message="boom")
    render_session_update(errored, Update.ERROR, bars.append, logs.append)
    assert logs == ["[error] boom"]


def test_exit_failure_texts_and_success_summary() -> None:
    _init_adw()
    from scanmole_gui.status import exit_failure_texts, success_summary

    heading, body = exit_failure_texts(6, None)
    assert heading == "No Pages Scanned"
    assert "ADF" in body

    heading, body = exit_failure_texts(99, "details here")
    assert heading == "Scan Failed"
    assert "99" in body and body.endswith("details here")

    assert success_summary(1, 0) == "1 page saved"
    assert success_summary(4, 2) == "4 pages saved · 2 blanks skipped"
