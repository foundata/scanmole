"""The ``--json`` event protocol: machine-readable JSON lines on standard output.

This writer owns *only* the compatibility boundary that frontends parse. Human
progress and diagnostics go through the :mod:`logging` module to standard error
instead, so the two never interleave on the same stream.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO


class EventWriter:
    """Emit one JSON object per line to standard output when enabled.

    When ``enabled`` is false (no ``--json``), every call is a no-op: the
    machine protocol is silent and only the logging-based human output remains.
    """

    def __init__(self, *, enabled: bool, stream: TextIO | None = None) -> None:
        """Configure the writer.

        Args:
            enabled: Whether to emit events. False mirrors a run without
                ``--json``.
            stream: Destination stream. Defaults to the current
                ``sys.stdout``.
        """
        self._enabled = enabled
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: str, **fields: object) -> None:
        """Write one ``{"event": ..., ...}`` line and flush.

        The ``event`` key is emitted first; ``fields`` supply the remaining
        payload documented for each event type.
        """
        if not self._enabled:
            return
        payload: dict[str, object] = {"event": event, **fields}
        json.dump(payload, self._stream, ensure_ascii=False)
        self._stream.write("\n")
        self._stream.flush()

    def error(self, message: str, *, code: int = 1) -> None:
        """Emit an ``error`` event carrying ``message`` and the exit ``code``.

        ``code`` mirrors the process exit status so frontends can classify the
        failure from the stream alone, without waiting on the process.
        """
        self.emit("error", message=message, code=code)
