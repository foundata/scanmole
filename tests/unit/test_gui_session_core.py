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


def test_automatic_size_emits_the_family_preference() -> None:
    chosen = request_argv(
        _request(page_size="auto", auto_size_preference="north-american"), "scanmole"
    )
    default = request_argv(_request(page_size="auto"), "scanmole")

    assert chosen[chosen.index("--auto-size-preference") + 1] == "north-american"
    assert default[default.index("--auto-size-preference") + 1] == "iso"


def test_fixed_size_omits_the_family_preference() -> None:
    argv = request_argv(
        _request(page_size="letter", auto_size_preference="north-american"),
        "scanmole",
    )

    assert "--auto-size-preference" not in argv


def test_request_snapshot_carries_the_preference() -> None:
    assert _request().auto_size_preference == "iso"
    chosen = _request(auto_size_preference="north-american")
    assert chosen.auto_size_preference == "north-american"


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


def test_waiting_event_marks_the_session_waiting() -> None:
    state = SessionState(drop_blanks=True)

    state, update = apply_event(
        state,
        {
            "event": "waiting",
            "sheets": 2,
            "pages": 4,
            "idle_seconds": 900,
            "manual_trigger": True,
        },
    )

    assert update is Update.WAITING
    assert state.waiting
    assert state.waiting_sheets == 2
    assert state.waiting_manual is True


def test_waiting_fields_parse_defensively() -> None:
    # Malformed fields must not crash or mislead: the sheet count falls
    # back to the locally counted pages, and only a real True is manual.
    state = SessionState(drop_blanks=True, pages=3)

    state, update = apply_event(
        state,
        {"event": "waiting", "sheets": "two", "manual_trigger": "yes"},
    )

    assert update is Update.WAITING
    assert state.waiting
    assert state.waiting_sheets == 3
    assert state.waiting_manual is False


def test_the_next_page_clears_the_waiting_state() -> None:
    state = SessionState(drop_blanks=True, pages=1)
    state, _ = apply_event(state, {"event": "waiting", "sheets": 1})

    state, update = apply_event(state, {"event": "page", "n": 2, "blank": False})

    assert update is Update.PAGE
    assert not state.waiting


def test_scan_done_clears_the_waiting_state() -> None:
    state = SessionState(drop_blanks=True, pages=2)
    state, _ = apply_event(state, {"event": "waiting", "sheets": 2})

    state, _ = apply_event(state, {"event": "scan_done", "total": 2, "kept": 2})

    assert not state.waiting


def test_unknown_event_kinds_stay_ignored_around_waiting() -> None:
    # The tolerance that lets old GUIs survive the new waiting event must
    # itself keep holding for whatever kind comes next.
    state = SessionState(drop_blanks=True)

    state, update = apply_event(state, {"event": "totally-new-kind", "x": 1})

    assert update is Update.NONE
    assert state == SessionState(drop_blanks=True)


def test_argv_omits_the_default_sheet_flow() -> None:
    # The stack default keeps the historic command line byte for byte.
    argv = request_argv(_request(), "scanmole")

    assert "--sheet-flow" not in argv


def test_argv_carries_a_non_default_sheet_flow() -> None:
    single = request_argv(_request(sheet_flow="single"), "scanmole")
    collect = request_argv(_request(sheet_flow="collect"), "scanmole")

    assert single[single.index("--sheet-flow") + 1] == "single"
    assert collect[collect.index("--sheet-flow") + 1] == "collect"
