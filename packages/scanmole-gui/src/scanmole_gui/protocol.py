"""Decoding of the frozen CLI JSON event protocol (no GTK imports).

One stdout line from ``scanmole --json`` is one JSON object per the frozen
protocol (see ARCHITECTURE.md). Anything else on stdout must never crash
the GUI: it is surfaced verbatim in the log instead, because losing a
stray diagnostic line is worse than showing it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

Event = dict[str, object]


@dataclass(frozen=True)
class RawLine:
    """A stdout line that is not a protocol event; shown in the log as-is."""

    text: str


def decode_stdout(line: str) -> Event | RawLine | None:
    """Classify one stdout line.

    Returns:
        The event mapping for a JSON object line, :class:`RawLine` for
        non-JSON or wrong-shaped JSON, ``None`` for blank lines.
    """
    line = line.strip()
    if not line:
        return None
    try:
        decoded = json.loads(line)
    except ValueError:
        return RawLine(line)
    if not isinstance(decoded, dict):
        return RawLine(line)
    return decoded


def event_kind(event: Event) -> str | None:
    """The event's ``event`` field, or ``None`` when it is not a string."""
    kind = event.get("event")
    return kind if isinstance(kind, str) else None
