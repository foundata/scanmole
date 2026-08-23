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


def test_the_result_bar_note_wraps_and_clears_with_the_state() -> None:
    # The detail beside it is shaped for a file name (monospace and
    # ellipsized), so an advisory sentence gets a wrapping label of its
    # own and never survives into the next state.
    _init_adw()
    from scanmole_gui.status import ResultBar

    bar = ResultBar(on_show=lambda: None, on_open=lambda: None)

    bar.set_state("success", "1 page saved", "out.pdf", note="Install jbig2enc")
    assert bar._note.get_visible() is True
    assert bar._note.get_text() == "Install jbig2enc"
    assert bar._note.get_wrap() is True

    bar.set_state("success", "1 page saved", "out.pdf")
    assert bar._note.get_visible() is False
    assert bar._note.get_text() == ""


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


def test_waiting_texts_use_sheet_singular_and_plural() -> None:
    _init_adw()
    from scanmole_gui.status import waiting_text

    assert waiting_text(0, manual=False) == "Insert the first sheet."
    assert waiting_text(1, manual=False) == "1 sheet scanned. Insert the next sheet."
    assert waiting_text(3, manual=False) == "3 sheets scanned. Insert the next sheet."
    assert (
        waiting_text(1, manual=True)
        == "1 sheet scanned. Place the next sheet, then press Next Sheet."
    )
    assert (
        waiting_text(0, manual=True) == "Place the first sheet, then press Next Sheet."
    )


def test_close_confirmation_counts_sheets_while_waiting_and_pages_otherwise() -> None:
    _init_adw()
    from scanmole_gui.session import SessionState
    from scanmole_gui.status import close_confirmation_text

    # A waiting collect run counts the sheets the engine grouped, so the
    # number matches the result bar the user was just looking at.
    heading, body = close_confirmation_text(
        SessionState(drop_blanks=True, pages=6, waiting=True, waiting_sheets=3)
    )
    assert heading == "Close and discard 3 scanned sheets?"
    assert "Press Finish" in body

    one, _body = close_confirmation_text(
        SessionState(drop_blanks=True, pages=2, waiting=True, waiting_sheets=1)
    )
    assert one == "Close and discard 1 scanned sheet?"

    # Mid-batch there is no Finish button, so the body must not name one.
    heading, body = close_confirmation_text(SessionState(drop_blanks=True, pages=2))
    assert heading == "Close and discard 2 scanned pages?"
    assert "Finish" not in body


def test_render_waiting_routes_to_the_waiting_bar() -> None:
    _init_adw()
    from scanmole_gui.session import SessionState, Update
    from scanmole_gui.status import render_session_update

    waits: list[tuple[str, bool]] = []
    state = SessionState(
        drop_blanks=True, waiting=True, waiting_sheets=2, waiting_manual=True
    )

    render_session_update(
        state,
        Update.WAITING,
        lambda title: None,
        lambda text: None,
        set_waiting_bar=lambda title, manual: waits.append((title, manual)),
    )

    assert waits == [
        ("2 sheets scanned. Place the next sheet, then press Next Sheet.", True)
    ]


def test_wait_actions_show_disable_on_click_and_clear_on_state() -> None:
    _init_adw()
    from scanmole_gui.status import ResultBar

    clicks: list[str] = []
    bar = ResultBar(
        on_show=lambda: None,
        on_open=lambda: None,
        on_next_sheet=lambda: clicks.append("next"),
        on_finish=lambda: clicks.append("finish"),
    )

    bar.set_state("running", "waiting")
    bar.show_wait_actions(next_sheet=True)
    assert bar._next_btn.get_visible() and bar._finish_btn.get_visible()

    bar._next_btn.emit("clicked")
    assert clicks == ["next"]
    assert bar._next_btn.get_sensitive() is False  # locked until the next update

    bar.show_wait_actions(next_sheet=True)  # the next waiting event re-arms
    assert bar._next_btn.get_sensitive() is True

    bar._finish_btn.emit("clicked")
    assert clicks == ["next", "finish"]
    assert bar._finish_btn.get_sensitive() is False
    assert bar._next_btn.get_sensitive() is False

    bar.set_state("idle", "Ready.")  # any state change clears the actions
    assert bar._next_btn.get_visible() is False
    assert bar._finish_btn.get_visible() is False

    # An automatic feeder wait shows Finish only.
    bar.set_state("running", "waiting")
    bar.show_wait_actions(next_sheet=False)
    assert bar._next_btn.get_visible() is False
    assert bar._finish_btn.get_visible() is True
