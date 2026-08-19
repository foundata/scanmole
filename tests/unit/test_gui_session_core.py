"""Characterization of the GUI scan-session core (no GTK, no processes).

Pins the contract extracted from the former ``MainWindow`` internals:
exact argv construction, tolerant protocol decoding, pure state reduction
and the completion decision, including malformed, wrong-shaped and
out-of-order input.
"""

from __future__ import annotations

from pathlib import Path

from scanmole_gui.protocol import RawLine, decode_stdout, event_kind
from scanmole_gui.request import ScanRequest, request_argv
from scanmole_gui.session import (
    SessionState,
    Update,
    apply_event,
    complete,
    mark_cancelled,
)


def _request(**overrides: object) -> ScanRequest:
    values: dict[str, object] = {
        "device": "epsonds:net:host",
        "source": "adf-duplex",
        "mode": "lineart",
        "resolution": 300,
        "page_size": "a4",
        "ocr": True,
        "lang": "deu+eng",
        "deskew": True,
        "drop_blanks": True,
        "output": "/data/scan.pdf",
    }
    values.update(overrides)
    return ScanRequest(**values)  # type: ignore[arg-type]


# ---- argv construction ----------------------------------------------------


def test_argv_matches_the_cli_contract_exactly() -> None:
    argv = request_argv(_request(), "scanmole")

    assert argv == [
        "scanmole",
        "--json",
        "-d",
        "epsonds:net:host",
        "--source",
        "adf-duplex",
        "--mode",
        "lineart",
        "-r",
        "300",
        "--page-size",
        "a4",
        "--ocr",
        "-l",
        "deu+eng",
        "--deskew",
        "-o",
        "/data/scan.pdf",
    ]


def test_argv_variants_cover_every_switch() -> None:
    argv = request_argv(
        _request(
            device=None,
            mode="lineart-auto",
            ocr=False,
            deskew=False,
            drop_blanks=False,
        ),
        "/opt/bin/scanmole",
    )

    assert argv[0] == "/opt/bin/scanmole"
    assert "-d" not in argv
    mode_at = argv.index("--mode")
    assert argv[mode_at : mode_at + 4] == [
        "--mode",
        "lineart",
        "--lineart-threshold",
        "auto",
    ]
    assert "--no-ocr" in argv and "-l" not in argv
    assert "--no-deskew" in argv
    assert "--keep-blanks" in argv


# ---- protocol decoding ----------------------------------------------------


def test_decode_classifies_stdout_lines() -> None:
    assert decode_stdout("   \n") is None
    assert decode_stdout("plain diagnostics") == RawLine("plain diagnostics")
    assert decode_stdout('["valid", "json", "wrong", "shape"]') == RawLine(
        '["valid", "json", "wrong", "shape"]'
    )
    assert decode_stdout('{"event": "page", "n": 1}\n') == {"event": "page", "n": 1}


def test_event_kind_requires_a_string() -> None:
    assert event_kind({"event": "done"}) == "done"
    assert event_kind({"event": 5}) is None
    assert event_kind({}) is None


# ---- session reduction ----------------------------------------------------


def _fold(
    events: list[dict[str, object]], drop_blanks: bool = True
) -> tuple[SessionState, list[Update]]:
    state = SessionState(drop_blanks=drop_blanks)
    updates = []
    for event in events:
        state, update = apply_event(state, event)
        updates.append(update)
    return state, updates


def test_a_normal_run_reduces_to_its_result() -> None:
    state, updates = _fold(
        [
            {"event": "start"},
            {"event": "page", "n": 1, "blank": False},
            {"event": "page", "n": 2, "blank": True},
            {"event": "scan_done", "total": 2, "kept": 1},
            {"event": "ocr_start"},
            {"event": "done", "output": "scan.pdf", "pages": 1},
        ]
    )

    assert updates == [
        Update.STARTED,
        Update.PAGE,
        Update.PAGE,
        Update.SCAN_DONE,
        Update.OCR_STARTED,
        Update.NONE,
    ]
    assert state.pages == 2 and state.blanks == 1
    assert state.total == 2 and state.kept == 1
    assert state.output == "scan.pdf" and state.result_pages == 1


def test_blanks_only_count_when_the_run_drops_them() -> None:
    keeping, _ = _fold([{"event": "page", "n": 1, "blank": True}], drop_blanks=False)
    dropping, _ = _fold([{"event": "page", "n": 1, "blank": True}], drop_blanks=True)

    assert keeping.blanks == 0
    assert dropping.blanks == 1


def test_wrong_shaped_fields_fall_back_locally() -> None:
    state, updates = _fold(
        [
            {"event": "page"},  # no page number: derive it
            {"event": "page", "n": "two"},  # wrong type: derive it
            {"event": "page", "n": True},  # bool is not a page number
            {"event": "scan_done"},  # counts fall back to what was seen
            {"event": 5},  # non-string kind: ignored
            {"event": "later-extension", "x": 1},  # unknown kind: ignored
        ]
    )

    assert state.pages == 3
    assert state.total == 3 and state.kept == 3
    assert updates[-2:] == [Update.NONE, Update.NONE]


def test_malformed_streams_cannot_produce_impossible_state() -> None:
    # Regression: string "blanks", duplicate page events, backward page
    # numbers and inflated summary counts must not yield negative pages,
    # double-counted blanks or kept > total.
    state, updates = _fold(
        [
            {"event": "page", "n": 2, "blank": "false"},  # a string is not blank
            {"event": "page", "n": 2, "blank": True},  # duplicate: ignored
            {"event": "page", "n": -4, "blank": True},  # backward: ignored
            {"event": "scan_done", "kept": 99, "total": -9},
        ]
    )

    assert state.pages == 2 and state.blanks == 0  # monotonic, nothing doubled
    assert state.total == 2  # -9 rejected: falls back to the pages seen
    assert state.kept == 2  # 99 clamped to the total
    assert updates[1:3] == [Update.NONE, Update.NONE]


def test_forward_page_jumps_are_accepted() -> None:
    # The engine's numbering is authoritative when it moves forward.
    state, _ = _fold(
        [
            {"event": "page", "n": 1, "blank": True},
            {"event": "page", "n": 4, "blank": True},
        ]
    )

    assert state.pages == 4 and state.blanks == 2


def test_out_of_order_scan_done_stays_consistent() -> None:
    state, _ = _fold([{"event": "scan_done"}])

    assert state.total == 0 and state.kept == 0


def test_error_events_are_reported_and_kept_for_the_exit() -> None:
    state, updates = _fold([{"event": "error", "message": "device on fire"}])
    silent, _ = _fold([{"event": "error"}])

    assert updates == [Update.ERROR]
    assert state.error_message == "device on fire"
    assert silent.error_message is None  # the UI substitutes its own text


# ---- completion -----------------------------------------------------------


def test_success_resolves_a_relative_output_against_the_run_folder() -> None:
    state, _ = _fold([{"event": "done", "output": "scan.pdf", "pages": 3}])

    outcome = complete(state, 0, Path("/data"))

    assert outcome.kind == "success"
    assert outcome.output == Path("/data/scan.pdf")
    assert outcome.pages == 3


def test_success_keeps_an_absolute_output_as_reported() -> None:
    state, _ = _fold([{"event": "done", "output": "/elsewhere/scan.pdf"}])

    outcome = complete(state, 0, Path("/data"))

    assert outcome.output == Path("/elsewhere/scan.pdf")


def test_success_without_output_reports_no_file() -> None:
    outcome = complete(SessionState(drop_blanks=True), 0, Path("/data"))

    assert outcome.kind == "success" and outcome.output is None


def test_cancellation_wins_over_any_exit_code() -> None:
    state = mark_cancelled(SessionState(drop_blanks=True))

    assert complete(state, 0, Path("/data")).kind == "cancelled"
    assert complete(state, 3, Path("/data")).kind == "cancelled"


def test_failure_carries_the_last_error_message() -> None:
    state, _ = _fold(
        [
            {"event": "page", "n": 1},
            {"event": "error", "message": "sane_start failed"},
        ]
    )

    outcome = complete(state, 3, Path("/data"))

    assert outcome.kind == "failure"
    assert outcome.exit_code == 3
    assert outcome.error_message == "sane_start failed"
    assert outcome.pages == 1  # falls back to the pages seen
