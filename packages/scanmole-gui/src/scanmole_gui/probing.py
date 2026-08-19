"""Advisory capability-probe state for the GUI (importable without GTK).

The GUI probes a device's capabilities after selection to gray out choices
the engine's negotiation marks as degraded or unsupported. Probes run in
worker threads; this module owns the pure bookkeeping: one probe at a time,
stale results rejected by generation token, results cached by device plus
applied settings. Everything here is advisory; the engine re-negotiates
authoritatively immediately before every scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scanmole.negotiation import Support

Settings = tuple[tuple[str, str], ...]
Snapshot = object  # dict[str, Capability] | None; opaque to the coordinator


@dataclass(frozen=True)
class ProbeRequest:
    """One advisory probe: a device with ordered applied settings."""

    device: str
    settings: Settings = ()

    @property
    def key(self) -> tuple[str, Settings]:
        """The cache key: device identity plus the applied settings."""
        return (self.device, self.settings)


@dataclass
class ProbeCoordinator:
    """Serializes advisory probes and rejects stale results.

    ``begin()`` hands out a generation token or queues the request behind a
    running probe (newest wins; intermediate requests are obsolete by
    definition). ``complete()`` stores the result when the token is still
    current and returns a queued follow-up for the caller to start.
    """

    _generation: int = 0
    _running: ProbeRequest | None = None
    _queued: ProbeRequest | None = None
    _cache: dict[tuple[str, Settings], Snapshot] = field(default_factory=dict)

    def begin(self, request: ProbeRequest) -> int | None:
        """Start a probe now (returns its token) or queue it (returns None).

        A settings-applied refinement never displaces a queued bare probe:
        the bare snapshot is its prerequisite (a device switch queued it,
        and assessing the new device with the old device's availability is
        exactly the bug this prevents). Dropping the refinement loses
        nothing, because it is re-derived from the newest selection when
        the bare snapshot is applied.
        """
        if self._running is not None:
            if (
                request.settings
                and self._queued is not None
                and not self._queued.settings
            ):
                return None
            self._queued = request
            return None
        self._running = request
        self._generation += 1
        return self._generation

    def complete(
        self, generation: int, snapshot: Snapshot
    ) -> tuple[bool, ProbeRequest | None]:
        """Report a finished probe.

        A stale completion (a slow worker outlived a newer ``begin()``)
        touches nothing: the running probe stays running and keeps its
        queued follow-up.

        Returns:
            ``(current, follow_up)``: whether the result is still current
            (stale results must be dropped by the caller) and a queued
            request the caller should ``begin()`` next, if any.
        """
        if generation != self._generation:
            return False, None
        if self._running is not None:
            self._cache[self._running.key] = snapshot
        self._running = None
        follow_up, self._queued = self._queued, None
        return True, follow_up

    def cached(self, request: ProbeRequest) -> tuple[bool, Snapshot]:
        """The cached snapshot for a request: ``(hit, snapshot)``."""
        if request.key in self._cache:
            return True, self._cache[request.key]
        return False, None

    def forget(self, device: str) -> None:
        """Drop cached snapshots of one device (e.g. after a scan)."""
        self._cache = {
            key: value for key, value in self._cache.items() if key[0] != device
        }


def selection_blocked(support: Support) -> bool:
    """Whether a choice with this support may be actively selected.

    NATIVE, EMULATED and UNKNOWN stay selectable (UNKNOWN is the documented
    best-effort contract); DEGRADED and UNSUPPORTED remain visible but not
    selectable, so a lossy path needs no warning dialog: it simply cannot be
    chosen. The CLI keeps allowing degraded runs with a warning.
    """
    return support in (Support.DEGRADED, Support.UNSUPPORTED)
