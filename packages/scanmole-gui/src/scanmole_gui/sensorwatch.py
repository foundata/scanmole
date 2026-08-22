"""Idle hardware-sensor watching: device-access serialization and arming.

Two GTK-free pieces behind the GUI's sensor polling. The
:class:`AdvisoryGate` serializes advisory device access so discovery,
capability probes and sensor polls never run concurrently against a
scanner: priority work (discovery, probes) waits briefly for the gate,
an optional sensor poll never waits and simply skips its tick. The
:class:`SensorArbiter` turns raw sensor observations into at most one
trigger per fresh edge: the first observation is a baseline that never
triggers (a latched button press from before must not start anything),
a button trigger needs a no-to-yes edge, insert-to-scan needs a fresh
paper transition, and an explicit button event wins over an insertion
seen in the same observation. Behavior measured on one device (latching
buttons) is never assumed: even a backend that keeps ``scan=yes``
across reads yields exactly one trigger per edge.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from scanmole.sensors import SensorSnapshot

PRIORITY_ACQUIRE_TIMEOUT_SECONDS = 30.0
"""Bound on priority work waiting for the gate.

A sensor read finishes in about a second and its probe timeout bounds
the rest; if the gate is still held past this, the holder is wedged and
the priority work proceeds without it rather than wedging too (its own
process supervision still applies either way)."""


class AdvisoryGate:
    """Serializes advisory access to the scanner across worker threads."""

    def __init__(self) -> None:
        self._freed = threading.Condition()
        self._owner: str | None = None
        self._priority_waiting = 0

    def acquire(
        self, kind: str, timeout: float = PRIORITY_ACQUIRE_TIMEOUT_SECONDS
    ) -> bool:
        """Blockingly acquire for priority work (discovery, probes).

        Returns whether the gate was actually acquired; ``False`` after
        the timeout means the caller proceeds ungated (and must not
        release).
        """
        deadline = time.monotonic() + timeout
        with self._freed:
            self._priority_waiting += 1
            try:
                while self._owner is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._freed.wait(remaining)
                self._owner = kind
                return True
            finally:
                self._priority_waiting -= 1

    def try_acquire(self, kind: str) -> bool:
        """Acquire without waiting; refused while priority work waits."""
        with self._freed:
            if self._owner is not None or self._priority_waiting:
                return False
            self._owner = kind
            return True

    def release(self) -> None:
        """Free the gate (only after a successful acquire)."""
        with self._freed:
            self._owner = None
            self._freed.notify_all()


@dataclass(frozen=True)
class Observation:
    """Fresh sensor edges in one observation (both may be set at once)."""

    button: bool = False
    insert: bool = False


class SensorArbiter:
    """Edge detection and arming for idle hardware sensors (pure)."""

    def __init__(self) -> None:
        self._baselined = False
        self._last_button: bool | None = None
        self._last_paper: bool | None = None
        self._offline = False

    def reset(self) -> None:
        """Forget everything; the next observation is a baseline again.

        Called when polling stops (a scan starts, the device changes):
        whatever latches accumulate in between are stale state, not
        requests.
        """
        self._baselined = False
        self._last_button = None
        self._last_paper = None
        self._offline = False

    def mark_offline(self) -> bool:
        """Record a failed read; ``True`` exactly once per outage.

        An outage also drops the baseline: after a power cycle the
        device's latches are stale and must not trigger on recovery.
        """
        first = not self._offline
        self._offline = True
        self._baselined = False
        return first

    def observe(self, snapshot: SensorSnapshot) -> Observation:
        """Fold one successful sensor read into the arbiter."""
        self._offline = False
        if not self._baselined:
            # The baseline never triggers: a latched button press or a
            # sheet already loaded when watching starts is state, not a
            # fresh request.
            self._baselined = True
            self._last_button = snapshot.scan
            self._last_paper = snapshot.page_loaded
            return Observation()
        button = False
        if snapshot.scan is not None:
            button = snapshot.scan and self._last_button is not True
            self._last_button = snapshot.scan
        insert = False
        if snapshot.page_loaded is not None:
            # An insertion needs an observed empty level to come from, so
            # a yes with no such evidence behind it (the option was never
            # readable) proves nothing. Unavailable reads in between do
            # not break the chain: paper absent at some point and present
            # now is an insertion whether or not the sensor answered
            # every poll along the way.
            insert = snapshot.page_loaded and self._last_paper is False
            self._last_paper = snapshot.page_loaded
        return Observation(button=button, insert=insert)
