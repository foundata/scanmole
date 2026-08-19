"""Pure scan-session state reduction (no GTK imports).

The session is a fold over the CLI's event stream plus one completion step
at process exit. Reduction is pure data-in/data-out: no widgets, no
translations, no I/O. ``MainWindow`` renders the returned state and
:class:`Update` hints into translated UI text and owns nothing of the
decision logic.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from scanmole_gui.protocol import Event, event_kind


def _count(value: object, fallback: int) -> int:
    """A defensive count: the protocol is frozen, but inputs are external.

    Only a real nonnegative integer is accepted (a bool is not a count);
    anything else falls back to the locally derived value.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


@dataclass(frozen=True)
class SessionState:
    """Everything a running scan has reported so far.

    ``blanks`` counts only when the session drops blanks (snapshotted in
    the request), matching what the engine actually skips. ``output`` and
    ``result_pages`` arrive with the ``done`` event; ``kept``/``total``
    with ``scan_done``.
    """

    drop_blanks: bool
    pages: int = 0
    blanks: int = 0
    kept: int | None = None
    total: int | None = None
    output: str | None = None
    result_pages: int | None = None
    error_message: str | None = None
    cancelled: bool = False


class Update(enum.Enum):
    """What a reduced event means for the UI; the state carries the data."""

    NONE = "none"
    STARTED = "started"
    PAGE = "page"
    SCAN_DONE = "scan-done"
    OCR_STARTED = "ocr-started"
    ERROR = "error"


def apply_event(state: SessionState, event: Event) -> tuple[SessionState, Update]:
    """Fold one protocol event into the session state.

    Unknown event kinds are ignored (the protocol may gain kinds; old GUIs
    must keep working) and fields of unexpected type or range fall back to
    their local derivation. The counters are monotonic and consistent no
    matter how malformed the stream: pages never go backwards (a duplicate
    or backward page number is ignored entirely, so its blank flag cannot
    count twice), a blank is only a real ``True``, and a reported keep
    count is clamped to the reported total.
    """
    kind = event_kind(event)
    if kind == "start":
        return state, Update.STARTED
    if kind == "page":
        number = event.get("n")
        if isinstance(number, bool) or not isinstance(number, int):
            number = state.pages + 1  # missing or mistyped: derive locally
        if number <= state.pages:
            return state, Update.NONE  # duplicate or backward: already counted
        blank = event.get("blank") is True and state.drop_blanks
        return (
            replace(
                state,
                pages=number,
                blanks=state.blanks + (1 if blank else 0),
            ),
            Update.PAGE,
        )
    if kind == "scan_done":
        total = _count(event.get("total"), state.pages)
        kept = _count(event.get("kept"), max(state.pages - state.blanks, 0))
        return (
            replace(state, total=total, kept=min(kept, total)),
            Update.SCAN_DONE,
        )
    if kind == "ocr_start":
        return state, Update.OCR_STARTED
    if kind == "done":
        output = event.get("output")
        return (
            replace(
                state,
                output=str(output) if output else None,
                result_pages=_count(event.get("pages"), state.pages),
            ),
            Update.NONE,
        )
    if kind == "error":
        message = event.get("message")
        return (
            replace(state, error_message=str(message) if message else None),
            Update.ERROR,
        )
    return state, Update.NONE


def mark_cancelled(state: SessionState) -> SessionState:
    """Record a user cancellation; the exit then reports it as such."""
    return replace(state, cancelled=True)


@dataclass(frozen=True)
class Completion:
    """The final outcome of a run, decided once at process exit.

    ``output`` is resolved against the run's output folder (the CLI may
    report a relative path). ``pages`` prefers the ``done`` event's count
    and falls back to the pages seen.
    """

    kind: Literal["cancelled", "success", "failure"]
    exit_code: int
    output: Path | None
    pages: int
    blanks: int
    error_message: str | None


def complete(state: SessionState, exit_code: int, run_folder: Path) -> Completion:
    """Reduce the exit of the scan subprocess into the final outcome.

    A cancelled run reports cancelled regardless of the exit code (the
    engine may still exit 0 after finishing its cleanup); exit 0 is a
    success even without an output path (nothing was produced, e.g. all
    pages blank yet tolerated); everything else is a failure explained by
    the last ``error`` event, if any.
    """
    if state.cancelled:
        kind: Literal["cancelled", "success", "failure"] = "cancelled"
    elif exit_code == 0:
        kind = "success"
    else:
        kind = "failure"
    output: Path | None = None
    if kind == "success" and state.output:
        output = Path(state.output)
        if not output.is_absolute():
            output = run_folder / output
    return Completion(
        kind=kind,
        exit_code=exit_code,
        output=output,
        pages=state.result_pages if state.result_pages is not None else state.pages,
        blanks=state.blanks,
        error_message=state.error_message,
    )
