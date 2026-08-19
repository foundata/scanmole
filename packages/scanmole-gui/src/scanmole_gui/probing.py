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

from scanmole.negotiation import (
    Support,
    advisory_faint_assessment,
    assess_mode,
    assess_source,
)
from scanmole.options import Capability

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


SOURCE_VALUES = ("flatbed", "adf", "adf-duplex", "adf-back")
"""The GUI's source choices, in row order (labels live in the window)."""

MODE_VALUES = ("lineart", "gray", "color", "lineart-auto")
"""The GUI's mode choices, in row order."""


@dataclass
class CapabilityUpdate:
    """One consolidated flow outcome for the window to render.

    ``None`` fields mean "unchanged". The window owns widgets, GLib
    scheduling, worker threads and translations; it renders this state,
    starts the requested worker and logs the announced lines.
    """

    start_probe: tuple[int, ProbeRequest] | None = None
    source_blocked: dict[str, str] | None = None
    mode_blocked: dict[str, str] | None = None
    select_source: str | None = None
    adopted_sole_source: str | None = None
    log_probe_failure: bool = False
    refresh: bool = False


class CapabilityFlow:
    """Staged advisory-probe orchestration for one window (no GTK).

    Owns the bare-before-source sequence, base-snapshot ownership per
    device, stale-result and queueing decisions (via the coordinator),
    availability computation and the source reconciliation policy. All
    of it stays advisory: the engine re-negotiates authoritatively at
    scan time, and probing never starts while a scan runs (the window
    passes that context explicitly).
    """

    def __init__(self, preferred_source: str = "adf-duplex") -> None:
        self._coordinator = ProbeCoordinator()
        self._base_snapshot: dict[str, Capability] | None = None
        self._base_device: str | None = None
        self._logged_failure = False
        self.preferred_source = preferred_source
        """The user's own source choice; a temporary sole-source adoption
        never overwrites it, so a capable scanner gets it back."""
        self.last_caps: dict[str, Capability] | None = None
        """The most recently applied snapshot (source-applied when the
        follow-up ran); feeds the window's resolution hint."""

    def select_device(
        self, device: str | None, scanning: bool, current_source: str
    ) -> CapabilityUpdate:
        """A device was selected: invalidate foreign state, probe bare.

        The invalidation happens even while scanning; only the probe
        itself is suppressed then.
        """
        if device != self._base_device:
            # Until the new device's own bare snapshot lands, nothing may
            # be assessed with the old one.
            self._base_snapshot = None
            self._base_device = None
        update = CapabilityUpdate()
        if device is None or scanning:
            return update
        self._launch(update, ProbeRequest(device), device, current_source)
        return update

    def change_source(
        self, device: str | None, scanning: bool, current_source: str, manual: bool
    ) -> CapabilityUpdate:
        """The source choice changed: refine mode-dependent options.

        A manual change states a preference; a programmatic
        reconciliation (sole-source adoption, preference restore) must
        not overwrite what the user actually wants.
        """
        if manual:
            self.preferred_source = current_source
        update = CapabilityUpdate(refresh=True)
        if (
            device is None
            or scanning
            or self._base_snapshot is None
            or self._base_device != device
        ):
            # No bare snapshot of *this* device yet (its probe may still
            # be queued behind another device's): the refinement would
            # judge it with foreign availability. The bare apply
            # re-derives it later.
            return update
        assessment = assess_source(self._base_snapshot, current_source)
        if assessment.backend_value is not None:
            self._launch(
                update,
                ProbeRequest(device, (("--source", assessment.backend_value),)),
                device,
                current_source,
            )
        return update

    def probe_completed(
        self,
        token: int,
        request: ProbeRequest,
        snapshot: Snapshot,
        device: str | None,
        current_source: str,
    ) -> CapabilityUpdate:
        """A worker finished: store, run the follow-up, apply if current."""
        update = CapabilityUpdate()
        current, follow_up = self._coordinator.complete(token, snapshot)
        if follow_up is not None:
            self._launch(update, follow_up, device, current_source)
        if not current or request.device != device:
            return update  # stale: the user moved on
        if request.settings and request.settings != self._settings_for(
            device, current_source
        ):
            # A refinement of a source the user has left: the coordinator
            # keeps it cached for a return to that source, but rendering
            # it now would let a foreign source govern the current
            # availability (until the current source's own queued probe
            # lands, or forever when none is running).
            return update
        follow = self._apply(update, request, snapshot, current_source)
        if update.select_source is not None:
            current_source = update.select_source
        self._launch(update, follow, device, current_source)
        return update

    def _settings_for(self, device: str | None, current_source: str) -> Settings | None:
        """The applied settings a refinement of the current source uses.

        ``None`` when no refinement is derivable (no bare snapshot of
        this device, or the source has no backend value): no completed
        source-applied probe can match then.
        """
        if self._base_snapshot is None or self._base_device != device:
            return None
        assessment = assess_source(self._base_snapshot, current_source)
        if assessment.backend_value is None:
            return None
        return (("--source", assessment.backend_value),)

    def _launch(
        self,
        update: CapabilityUpdate,
        request: ProbeRequest | None,
        device: str | None,
        current_source: str,
    ) -> None:
        """Resolve cached requests inline; start or queue the first miss."""
        while request is not None:
            hit, snapshot = self._coordinator.cached(request)
            if hit:
                if request.device != device:
                    return  # stale: the user moved on
                request = self._apply(update, request, snapshot, current_source)
                if update.select_source is not None:
                    current_source = update.select_source
                continue
            token = self._coordinator.begin(request)
            if token is not None:
                update.start_probe = (token, request)
            return  # queued (or started) behind the coordinator

    def _apply(
        self,
        update: CapabilityUpdate,
        request: ProbeRequest,
        snapshot: Snapshot,
        current_source: str,
    ) -> ProbeRequest | None:
        """Fold one snapshot into availability state; return the follow-up."""
        caps = snapshot if isinstance(snapshot, dict) else None
        if caps is None and not self._logged_failure:
            self._logged_failure = True
            update.log_probe_failure = True
        follow: ProbeRequest | None = None
        if not request.settings:
            # Bare snapshot: source availability, then refine the modes
            # with the currently selected source applied.
            self._base_snapshot = caps
            self._base_device = request.device if caps is not None else None
            blocked: dict[str, str] = {}
            for value in SOURCE_VALUES:
                assessment = assess_source(caps, value)
                if selection_blocked(assessment.support):
                    blocked[value] = assessment.consequence
            update.source_blocked = blocked
            target, adopted = self._reconcile(blocked, current_source)
            if adopted is not None:
                update.adopted_sole_source = adopted
            if target is not None and target != current_source:
                update.select_source = target
                current_source = target
            selected = assess_source(caps, current_source)
            if caps is not None and selected.backend_value is not None:
                follow = ProbeRequest(
                    request.device, (("--source", selected.backend_value),)
                )
        self.last_caps = caps
        mode_blocked: dict[str, str] = {}
        for value in MODE_VALUES:
            # The faint mode takes the optimistic advisory verdict: a
            # visible native-enhancement signature keeps it selectable,
            # and the engine confirms the path at scan time.
            assessment = (
                advisory_faint_assessment(caps)
                if value == "lineart-auto"
                else assess_mode(caps, value)
            )
            if selection_blocked(assessment.support):
                mode_blocked[value] = assessment.consequence
        update.mode_blocked = mode_blocked
        update.refresh = True
        return follow

    def _reconcile(
        self, blocked: dict[str, str], current_source: str
    ) -> tuple[str | None, str | None]:
        """The reconciliation target for new availability, if any.

        The preferred source wins whenever the device offers it. When it
        is blocked and exactly one source remains selectable (the
        ScanSnap iX100 offers ADF Front alone), that sole source is
        adopted so Start stays usable instead of demanding a pointless
        click; the stored preference is untouched. While a real choice
        remains, nothing is changed silently.
        """
        available = [value for value in SOURCE_VALUES if value not in blocked]
        if self.preferred_source in available:
            return self.preferred_source, None
        if current_source in blocked and len(available) == 1:
            return available[0], available[0]
        return None, None
