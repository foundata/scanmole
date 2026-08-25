"""Asynchronous device coordination for the GUI window (GLib, no widgets).

One controller owns the lifecycle glue between the GTK-free policy
modules and the main loop: worker threads, the discovery and sensor poll
timers, staleness decisions and the pause/resume around a scan. The
policies themselves stay where they are — :mod:`scanmole_gui.discovery`
parses listings and decides compatibility, :mod:`scanmole_gui.probing`
owns the staged capability flow, :mod:`scanmole_gui.advisory` supervises
the advisory children, :mod:`scanmole_gui.sensorwatch` serializes device
access and arms sensor edges — this module only composes them.

The window supplies its current state through one snapshot callback
(:class:`DeviceContext`) and receives typed outcomes back; every
user-facing, translated string is reconstructed in the window. GLib is
used for main-loop scheduling only: nothing here imports Gtk or holds a
widget reference, so no late worker result can touch a dead window.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from gi.repository import GLib

from scanmole.external import run_command
from scanmole.negotiation import (
    ADVISORY_PROBE_TIMEOUT_SECONDS,
    probe_snapshot,
)
from scanmole.options import Capability
from scanmole.sensors import (
    SENSOR_PROBE_TIMEOUT_SECONDS,
    SensorSnapshot,
    assess_sensors,
)
from scanmole_gui.advisory import AdvisoryCommands
from scanmole_gui.discovery import evaluate_listing, parse_version
from scanmole_gui.probing import CapabilityFlow, CapabilityUpdate, ProbeRequest
from scanmole_gui.sensorwatch import AdvisoryGate, SensorArbiter

LOGGER = logging.getLogger(__name__)

DEVICE_POLL_SECONDS = 15
"""Pause between device searches while no scanner has been found.

Counted from the end of the previous search, so slow probes never shrink
the quiet gap. A probe costs a second or two of backend I/O plus discovery
traffic (sane-airscan emits mDNS/WSD queries): cheap at this cadence, so no
backoff; a suspended (minimized/hidden) window defers instead.
"""

DEVICE_PRESENCE_POLL_SECONDS = 45
"""Pause between presence checks while a scanner is on the list.

The same passive discovery, run forever: it notices the selected device
disappearing (standby, unplugged) and additional scanners appearing,
without ever opening a device (an open would keep it from sleeping).
Presence changes are rare, so the cadence is slower than the empty-list
pickup, and an unchanged result is applied quietly: no widget writes,
no renegotiation, no result-bar repaint.
"""

SENSOR_POLL_SECONDS = 2.5
"""Pause between completed idle sensor polls.

A read costs well under a second on measured hardware and a button
press latches until read there, so this cadence sees single presses
without churning the device; the engine's own collect wait polls
faster because a sheet is imminently expected there.
"""


@dataclass(frozen=True)
class DeviceContext:
    """One snapshot of the window state device coordination reads.

    Asked for freshly whenever the controller needs current state; it is
    never cached, and the controller never reaches into the form.
    ``visible`` and ``suspended`` are distinct on purpose and either one
    defers advisory polling: ``visible`` is the widget's own answer (an
    ordinarily hidden window), ``suspended`` the compositor's (GTK 4.12+;
    older runtimes never report it). A hidden window additionally
    consumes an in-flight sensor edge without scanning, and showing it
    re-baselines before a latch from the hidden period can trigger.
    Whether a scan runs or the window still lives is controller state
    (:meth:`DeviceFlow.pause_for_scan`, :meth:`DeviceFlow.stop`), not
    context, so the two sources of truth cannot disagree.
    """

    selected_device: str | None
    remembered_device: str
    """The persisted device preference: the reselection fallback while
    no row is selected (startup, or everything disappeared)."""
    source: str
    sensor_prefs: tuple[str, bool]
    start_allowed: bool
    visible: bool
    suspended: bool


class DiscoveryFailure(Enum):
    """Why one device search produced no usable result."""

    INCOMPATIBLE_CLI = "incompatible-cli"
    FAILED_EXIT = "failed-exit"
    CLI_MISSING = "cli-missing"
    TIMED_OUT = "timed-out"
    OS_ERROR = "os-error"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class ListingOutcome:
    """The decided result of one device search, ready to render.

    Carries exactly what the window needs to reproduce the current UI:
    it reconstructs the localized failure text from the typed
    ``failure`` kind and its detail fields, never from worker-formatted
    strings. ``unchanged`` marks a quiet presence check whose result
    matches the current list: nothing may be rendered for it.
    """

    devices: list[dict[str, str]] = field(default_factory=list)
    prefer: str = ""
    """The reselection target captured when the search started: the
    then-selected device, or the persisted preference."""
    unchanged: bool = False
    vanished: bool = False
    """Whether the previously selected device dropped off the list."""
    cli_version: str | None = None
    needed: str | None = None
    """The CLI version requirement when the GUI cannot drive it."""
    failure: DiscoveryFailure | None = None
    failed_exit: int | None = None
    error_detail: str = ""
    poll_scheduled: bool = False
    """Whether the next automatic quiet search was armed."""


@dataclass(frozen=True)
class SensorTrigger:
    """One consumed sensor edge that may start a scan.

    The window resolves ``mapping`` (``same`` means the form's current
    sheet flow) and re-checks its authoritative Start predicate before
    launching; a trigger it declines is consumed, never queued.
    """

    mapping: str
    reason: str


@dataclass(frozen=True)
class _SearchResult:
    """What the discovery worker hands back to the main loop.

    Pure data: the worker never touches live controller state, so a
    cancelled worker resuming past a takeover cannot overwrite what a
    newer search owns. ``cli_blocked`` is ``None`` where no listing was
    evaluated (the command itself failed): the previous verdict stands.
    """

    devices: list[dict[str, str]] = field(default_factory=list)
    cli_version: str | None = None
    cli_blocked: bool | None = None
    needed: str | None = None
    failure: DiscoveryFailure | None = None
    failed_exit: int | None = None
    error_detail: str = ""


class DeviceFlow:
    """Owns asynchronous device coordination for one window.

    Lifecycle: :meth:`start` once the UI exists, transition methods as
    the window's inputs move, :meth:`pause_for_scan` and
    :meth:`resume_after_scan` around a scan (the failed-runner-start
    path resumes the same way), :meth:`stop` exactly once the window
    releases its device work. After ``stop()`` no callback fires again.
    """

    def __init__(
        self,
        *,
        scanmole: str,
        context: Callable[[], DeviceContext],
        on_searching: Callable[[], None],
        on_listing: Callable[[ListingOutcome], None],
        on_capabilities: Callable[[CapabilityUpdate], None],
        on_trigger: Callable[[SensorTrigger], None],
        on_log: Callable[[str], None],
    ) -> None:
        self._scanmole = scanmole
        self._context = context
        self._on_searching = on_searching
        self._on_listing = on_listing
        self._on_capabilities = on_capabilities
        self._on_trigger = on_trigger
        self._on_log = on_log
        self._advisory = AdvisoryCommands()
        self._flow = CapabilityFlow()
        # The gate serializes advisory device access (discovery and
        # probes have priority, sensor polls skip their tick), the
        # arbiter turns reads into one trigger per fresh edge.
        self._gate = AdvisoryGate()
        self._arbiter = SensorArbiter()
        self._devices: list[dict[str, str]] = []
        self._obscured = False
        self._searching = False
        # Bumped when a search starts and when a takeover or stop
        # invalidates it: only the completion holding the current token
        # owns the latch and may apply its result.
        self._search_token = 0
        self._paused = False
        self._stopped = False
        self._device_poll_id: int | None = None
        self._sensor_poll_id: int | None = None
        self._sensor_poll_busy = False
        self._cli_version: str | None = None
        self._cli_blocked = False

    # ------------------------------------------------- window-read state

    @property
    def searching(self) -> bool:
        """Whether a device search is running (feeds the Start predicate)."""
        return self._searching

    @property
    def cli_blocked(self) -> bool:
        """Whether the CLI's major version blocks scanning."""
        return self._cli_blocked

    @property
    def cli_version(self) -> str | None:
        """The driven CLI's version string, once probed (About dialog)."""
        return self._cli_version

    @property
    def preferred_source(self) -> str:
        """The user's own source choice (persisted by the window)."""
        return self._flow.preferred_source

    @preferred_source.setter
    def preferred_source(self, value: str) -> None:
        self._flow.preferred_source = value

    @property
    def last_caps(self) -> dict[str, Capability] | None:
        """The advisory snapshot behind the window's resolution hint."""
        return self._flow.last_caps

    # ------------------------------------------------------- transitions

    def start(self) -> None:
        """Initial discovery, once the window's widgets exist."""
        self.refresh()

    def refresh(self, *, quiet: bool = False) -> None:
        """Start an asynchronous device search in a worker thread.

        A quiet search (the background presence check) paints nothing at
        start and applies an unchanged result without touching a widget;
        only a real change goes through the full apply.
        """
        if self._stopped or self._paused or self._searching:
            return
        self._searching = True
        self._search_token += 1
        context = self._context()
        if not quiet:
            self._on_searching()
        prefer = context.selected_device or context.remembered_device
        self._advisory.spawn_worker(
            self._devices_worker,
            prefer,
            self._cli_version,
            self._advisory.generation,
            self._search_token,
            quiet,
        )

    def device_changed(self) -> None:
        """A device was selected: invalidate foreign state, probe it."""
        if self._stopped:
            return
        # Another device's latches are meaningless here: the next
        # observation of the new device is a baseline.
        self._arbiter.reset()
        self._negotiate()

    def source_changed(self, manual: bool) -> None:
        """The source choice changed: refine mode-dependent options.

        Whether the change is manual (a preference) or programmatic (a
        reconciliation select) is widget-callback context only the form
        has; the GTK-free flow owns everything else.
        """
        if self._stopped:
            return
        # Sensor evidence is source-dependent state: what the previous
        # source latched says nothing about this one, so the next
        # observation is a baseline again.
        self._arbiter.reset()
        context = self._context()
        update = self._flow.change_source(
            context.selected_device, self._paused, context.source, manual=manual
        )
        self._dispatch(update)

    def settings_reset(self) -> None:
        """The saved form was reset: negotiate again, exactly like startup.

        The defaults may name choices the connected scanner blocks (the
        duplex default on a front-only feeder); the sole-source adoption
        can only move the selection off a blocked default through a
        fresh negotiation. Cached snapshots make this instant; without a
        device it is a no-op.
        """
        if self._stopped:
            return
        self._negotiate()

    def preferences_changed(self) -> None:
        """A sensor trigger preference changed: re-evaluate the poller."""
        if self._stopped:
            return
        self._schedule_sensor_poll()

    def view_state_changed(self) -> None:
        """The window was hidden, shown, suspended or resumed.

        One transition for every way a window stops being watchable:
        while hidden or compositor-suspended the tick skips, so no read
        consumes a press made in between; whatever latched is stale
        state, not a request, and the first observation after the window
        is watchable again is a discarded baseline. Entering an obscured
        state changes nothing by itself, and a runtime that cannot
        notify suspension (GTK before 4.12) simply never reports that
        pair of transitions.
        """
        if self._stopped:
            return
        context = self._context()
        if not context.visible or context.suspended:
            self._obscured = True
            return
        if self._obscured:
            self._obscured = False
            self._arbiter.reset()
        self._schedule_sensor_poll()

    def pause_for_scan(self) -> bool:
        """The scan takeover: leave the device to the starting runner.

        Atomic from the window's perspective: the idle sensor poller
        stops first (a press latched during the run must resume as
        baseline state, never as a trigger), the advisory children are
        cancelled and their workers joined boundedly, the search latch
        clears, and the cancelled capability state resets so nothing
        queues behind a probe whose completion will never arrive.

        Returns:
            Whether advisory work went idle; ``False`` means a worker
            thread is still wedged (its child group is dead either way)
            and the window keeps its existing warning line.
        """
        self._paused = True
        self._stop_sensor_polling()
        idle = self._advisory.cancel_pending()
        # The takeover owns the cancelled search's cleanup: it clears
        # the latch itself and invalidates the token, so the cancelled
        # completion has nothing left to do.
        self._searching = False
        self._search_token += 1
        self._flow.reset()
        return idle

    def resume_after_scan(self) -> None:
        """Resume device coordination after the scan freed the device.

        Used after a normal scan exit and immediately after a failed
        runner start: fresh capability negotiation for the current
        selection first, then the poll attempt, so sensor polling can
        only resume from accepted source-matched evidence, never from
        the stale caps the takeover's reset made underivable.
        """
        if self._stopped:
            return
        self._paused = False
        self._arbiter.reset()
        self._negotiate()
        self._schedule_sensor_poll()

    def stop(self) -> None:
        """Release permanently (window close, application shutdown).

        Idempotent. No advisory child survives, and no worker result,
        timer or trigger reaches the window afterwards.
        """
        self._stopped = True
        self._searching = False
        self._search_token += 1
        self._stop_sensor_polling()
        if self._device_poll_id is not None:
            GLib.source_remove(self._device_poll_id)
            self._device_poll_id = None
        self._advisory.cancel_pending(close=True)

    # ----------------------------------------------------------- devices

    def _devices_worker(
        self,
        prefer: str,
        cli_version: str | None,
        generation: int,
        token: int,
        quiet: bool = False,
    ) -> None:
        """Worker thread: query devices and hand results back typed.

        The parsing and the compatibility decision live in the GTK-free
        :mod:`scanmole_gui.discovery`; this thread only runs the command
        (through the supervised engine helper, so a wedged backend probe
        cannot leave descendants behind on timeout) and types the
        outcome. No user-facing text is formatted here, and no live
        controller state is touched: ``cli_version`` came in with the
        spawn, everything learned goes back through the result, and the
        main loop applies it only while ``generation`` and ``token`` are
        still current.
        """
        adopt = self._advisory.adopter(generation)
        # Priority access: an in-flight sensor poll finishes first, so the
        # discovery command never races it on the device.
        held = self._gate.acquire("discovery")
        if cli_version is None:
            cli_version = self._probe_cli_version(adopt)
            if cli_version is not None:
                # Published on its own rather than with the listing below:
                # which engine is installed is a fact about the
                # installation, and asking a device for it can take the
                # full listing timeout on a network backend. The About
                # dialog would name no engine for all of that time.
                GLib.idle_add(self._adopt_cli_version, cli_version, generation, token)
        devices: list[dict[str, str]] = []
        cli_blocked: bool | None = None
        needed: str | None = None
        failure: DiscoveryFailure | None = None
        failed_exit: int | None = None
        error_detail = ""
        try:
            result = run_command(
                [self._scanmole, "--list-devices", "--json"],
                timeout_seconds=120,
                on_spawn=adopt,
            )
            if result.stderr.strip():
                GLib.idle_add(self._log_line, result.stderr.strip(), generation, token)
            listing = evaluate_listing(result.stdout, result.returncode)
            if listing.cli_version is not None:
                cli_version = listing.cli_version
            cli_blocked = listing.needed is not None
            devices = listing.devices
            needed = listing.needed
            if listing.needed is not None:
                failure = DiscoveryFailure.INCOMPATIBLE_CLI
            elif listing.failed_exit is not None:
                failure = DiscoveryFailure.FAILED_EXIT
                failed_exit = listing.failed_exit
        except FileNotFoundError:
            failure = DiscoveryFailure.CLI_MISSING
        except subprocess.TimeoutExpired:
            failure = DiscoveryFailure.TIMED_OUT
        except OSError as exc:
            failure = DiscoveryFailure.OS_ERROR
            error_detail = str(exc)
        except Exception:  # defensive: the UI must never stay dead
            LOGGER.debug("device search failed unexpectedly", exc_info=True)
            failure = DiscoveryFailure.UNEXPECTED
        finally:
            if held:
                self._gate.release()
            # Always reaches the main loop: the owning completion clears
            # the latch there, or the refresh button and the "Searching
            # for scanners…" subtitle would stay stuck forever.
            GLib.idle_add(
                self._apply_listing,
                _SearchResult(
                    devices=devices,
                    cli_version=cli_version,
                    cli_blocked=cli_blocked,
                    needed=needed,
                    failure=failure,
                    failed_exit=failed_exit,
                    error_detail=error_detail,
                ),
                prefer,
                generation,
                token,
                quiet,
            )

    def _adopt_cli_version(self, version: str, generation: int, token: int) -> None:
        """Record the probed engine version, unless cancelled or superseded.

        The same ownership rule the listing follows: a search invalidated
        underneath contributes nothing, however far it had got.
        """
        if (
            self._stopped
            or generation != self._advisory.generation
            or token != self._search_token
        ):
            return
        self._cli_version = version

    def _probe_cli_version(
        self, adopt: Callable[[subprocess.Popen[bytes]], None]
    ) -> str | None:
        """Return the supervised CLI's version string, or ``None``."""
        try:
            result = run_command(
                [self._scanmole, "--version"],
                timeout_seconds=30,
                on_spawn=adopt,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_version(result.stdout)

    def _log_line(self, text: str, generation: int, token: int) -> None:
        """Forward one worker diagnostic, unless cancelled or superseded."""
        if (
            self._stopped
            or generation != self._advisory.generation
            or token != self._search_token
        ):
            return
        self._on_log(text)

    def _apply_listing(
        self,
        search: _SearchResult,
        prefer: str,
        generation: int,
        token: int,
        quiet: bool,
    ) -> None:
        """Decide one search result on the main loop and hand it over.

        A result cancelled or superseded underneath changes nothing at
        all: the invalidating owner (takeover, stop, the newer search)
        already cleaned up its own state, and only the completion that
        holds the current token may clear the latch or touch the
        compatibility verdict. A quiet result matching the current list
        is marked ``unchanged``: the window applies it without a widget
        write, and no renegotiation runs.
        """
        if (
            self._stopped
            or generation != self._advisory.generation
            or token != self._search_token
        ):
            return
        self._searching = False
        if search.cli_version is not None:
            self._cli_version = search.cli_version
        if search.cli_blocked is not None:
            self._cli_blocked = search.cli_blocked
        unchanged = (
            quiet
            and search.failure is None
            and not self._cli_blocked
            and search.devices == self._devices
        )
        # The selected device dropping off the list is worth a word:
        # standby and unplugging look identical here, and the poll
        # reselects it silently once it reappears.
        vanished = (
            not unchanged
            and bool(prefer)
            and any(d.get("device") == prefer for d in self._devices)
            and not any(d.get("device") == prefer for d in search.devices)
        )
        if not unchanged:
            self._devices = search.devices
        # Presence polling never stops (issue #7 covered plugging in after
        # start; the same passive search now also notices standby and new
        # devices): fast pickup while the list is empty, a slow quiet check
        # while a device is present. One-shot chain, so the pause counts
        # from the end of a search, and every completed search (auto or
        # manual refresh) restarts the countdown.
        if self._device_poll_id is not None:
            GLib.source_remove(self._device_poll_id)
            self._device_poll_id = None
        poll_scheduled = False
        if not self._cli_blocked:
            self._device_poll_id = GLib.timeout_add_seconds(
                DEVICE_POLL_SECONDS
                if not self._devices
                else DEVICE_PRESENCE_POLL_SECONDS,
                self._poll_tick,
            )
            poll_scheduled = True
        self._on_listing(
            ListingOutcome(
                devices=list(search.devices),
                prefer=prefer,
                unchanged=unchanged,
                vanished=vanished,
                cli_version=search.cli_version,
                needed=search.needed,
                failure=search.failure,
                failed_exit=search.failed_exit,
                error_detail=search.error_detail,
                poll_scheduled=poll_scheduled,
            )
        )
        if not unchanged and self._devices:
            self._negotiate()
        # Every applied search is also a predicate transition: the idle
        # sensor poller may start or stop; the attempt checks everything.
        self._schedule_sensor_poll()

    def _poll_tick(self) -> bool:
        """One automatic quiet re-search: pickup while empty, presence check.

        Discovery stays passive (no device is opened), so this can run
        forever without keeping a scanner from its standby.
        """
        self._device_poll_id = None
        if self._stopped or self._cli_blocked:
            return bool(GLib.SOURCE_REMOVE)
        context = self._context()
        if self._paused or self._searching or context.suspended or not context.visible:
            # Transient: defer a full interval; the next completed search
            # would reschedule anyway, this covers scans and hidden windows.
            self._device_poll_id = GLib.timeout_add_seconds(
                DEVICE_POLL_SECONDS
                if not self._devices
                else DEVICE_PRESENCE_POLL_SECONDS,
                self._poll_tick,
            )
            return bool(GLib.SOURCE_REMOVE)
        if not self._devices:
            self._on_log("[gui] no scanner yet — searching again")
        self.refresh(quiet=True)
        return bool(GLib.SOURCE_REMOVE)  # the search result schedules the next

    # -------------------------------------------- capability negotiation

    def _negotiate(self) -> None:
        """Kick off an advisory capability probe for the selected device.

        Two stages: a bare probe derives source availability, a follow-up
        with the negotiated source applied refines the mode-dependent
        options. The GTK-free flow serializes probes, drops stale results
        and never probes while a scan owns the device. Advisory only: the
        engine re-negotiates before every scan.
        """
        context = self._context()
        update = self._flow.select_device(
            context.selected_device, self._paused, context.source
        )
        self._dispatch(update)

    def _dispatch(self, update: CapabilityUpdate) -> None:
        """Start the requested worker, hand the update over, poll maybe.

        The window renders the update synchronously in its callback, so
        the poll attempt afterwards already sees the new selection-block
        state; a probe this update started keeps the poller off either
        way until its accepted result lands.
        """
        if update.start_probe is not None:
            token, request = update.start_probe
            self._advisory.spawn_worker(
                self._probe_worker, token, request, self._advisory.generation
            )
        self._on_capabilities(update)
        self._schedule_sensor_poll()

    def _probe_worker(self, token: int, request: ProbeRequest, generation: int) -> None:
        held = self._gate.acquire("probe")
        try:
            snapshot = probe_snapshot(
                request.device,
                request.settings,
                ADVISORY_PROBE_TIMEOUT_SECONDS,
                on_spawn=self._advisory.adopter(generation),
            )
        finally:
            if held:
                self._gate.release()
        GLib.idle_add(self._probe_done, token, request, snapshot, generation)

    def _probe_done(
        self, token: int, request: ProbeRequest, snapshot: object, generation: int
    ) -> None:
        if self._stopped or generation != self._advisory.generation:
            return  # cancelled underneath: the result must not render
        context = self._context()
        update = self._flow.probe_completed(
            token, request, snapshot, context.selected_device, context.source
        )
        if update.sensor_caps is not None:
            # A live probe read the device and consumed any sensor latch,
            # and the flow accepted it for the current selection: fold it
            # into the arbiter exactly once. A rejected result describes
            # another device or a source the user has left, and a cached
            # snapshot is not fresh evidence at all.
            self._observe(assess_sensors(update.sensor_caps), context, defer=True)
        self._dispatch(update)

    # ------------------------------------------------ idle sensor polling

    def _polling_wanted(self, context: DeviceContext) -> bool:
        """Whether an idle sensor poll should run right now.

        Capability-driven and preference-gated: the controller alive and
        unpaused, Start currently allowed (which covers the running
        scan, an active search, a blocked CLI and the selected device),
        no capability probe active, and an enabled trigger whose sensor
        the device's last advisory listing actually carries. Pairing the
        two matters: a paper level is no evidence of a scan button, so a
        button mapping alone must not poll a device that has none. Never
        a device list.
        """
        mapping, insert = context.sensor_prefs
        if self._stopped or self._paused:
            return False
        if not context.start_allowed:
            return False
        if self._flow.probe_active:
            return False
        caps = self._flow.last_caps
        if caps is None:
            return False
        sensors = assess_sensors(caps)
        return (mapping != "off" and sensors.scan is not None) or (
            insert and sensors.page_loaded is not None
        )

    def _schedule_sensor_poll(self) -> None:
        """Arm the next idle poll if wanted and none is armed or running."""
        if self._sensor_poll_id is not None or self._sensor_poll_busy:
            return
        if not self._polling_wanted(self._context()):
            return
        self._sensor_poll_id = GLib.timeout_add(
            round(SENSOR_POLL_SECONDS * 1000), self._sensor_tick
        )

    def _sensor_tick(self) -> bool:
        """One poll attempt; the completion callback schedules the next."""
        self._sensor_poll_id = None
        context = self._context()
        if not self._polling_wanted(context):
            return bool(GLib.SOURCE_REMOVE)
        if (
            not context.visible
            or context.suspended
            or not self._gate.try_acquire("sensor")
        ):
            # Transient (hidden or suspended window, discovery or a probe
            # owns the device): skip this tick, try again a full interval
            # later; the scanner is never opened for a window nobody sees.
            self._schedule_sensor_poll()
            return bool(GLib.SOURCE_REMOVE)
        device = context.selected_device
        if device is None:  # pragma: no cover -- the predicate guards this
            self._gate.release()
            return bool(GLib.SOURCE_REMOVE)
        self._sensor_poll_busy = True
        self._advisory.spawn_worker(
            self._sensor_worker,
            device,
            self._flow.sensor_settings(device, context.source),
            self._advisory.generation,
        )
        return bool(GLib.SOURCE_REMOVE)

    def _sensor_worker(
        self, device: str, settings: tuple[tuple[str, str], ...], generation: int
    ) -> None:
        """Worker thread: one gated sensor read, result to the main loop.

        ``settings`` applies the selected source, exactly as the engine's
        own sensor reads do: a paper level read from the device's default
        source would answer a question nobody asked.
        """
        try:
            caps = probe_snapshot(
                device,
                settings,
                SENSOR_PROBE_TIMEOUT_SECONDS,
                on_spawn=self._advisory.adopter(generation),
            )
        finally:
            self._gate.release()
        snapshot = assess_sensors(caps) if caps is not None else None
        GLib.idle_add(self._sensor_done, snapshot, generation)

    def _sensor_done(self, snapshot: SensorSnapshot | None, generation: int) -> None:
        """Fold one poll result on the main loop and arm the next poll."""
        self._sensor_poll_busy = False
        if self._stopped or generation != self._advisory.generation:
            return  # cancelled underneath (scan takeover, window close)
        if snapshot is None:
            # A device-open failure: stop watching, let ordinary discovery
            # take over; the arbiter keeps the outage to one log line.
            if self._arbiter.mark_offline():
                self._on_log(
                    "[gui] scanner stopped answering sensor reads; "
                    "searching for devices"
                )
            if not self._paused and not self._searching:
                self.refresh()
            return
        self._observe(snapshot, self._context())
        self._schedule_sensor_poll()

    def _observe(
        self, snapshot: SensorSnapshot, context: DeviceContext, *, defer: bool = False
    ) -> None:
        """Run one observation through the arbiter and map its triggers.

        ``defer`` emits a resulting trigger from an idle callback
        instead of inline: a live capability probe's evidence arrives in
        the middle of flow bookkeeping that must finish first.
        """
        observation = self._arbiter.observe(snapshot)
        mapping, insert = context.sensor_prefs
        trigger: SensorTrigger | None = None
        # An explicit button mapping wins over an insertion seen in the
        # same observation; either trigger is consumed here, and a
        # blocked Start ignores it without queueing anything.
        if observation.button and mapping != "off":
            trigger = SensorTrigger(mapping=mapping, reason="hardware button")
        elif observation.insert and insert:
            trigger = SensorTrigger(mapping="same", reason="paper inserted")
        if trigger is None:
            return
        if not context.start_allowed or not context.visible:
            return  # consumed and ignored, never queued
        if defer:
            chosen = trigger
            GLib.idle_add(lambda: self._emit_trigger(chosen))
        else:
            self._emit_trigger(trigger)

    def _emit_trigger(self, trigger: SensorTrigger) -> bool:
        """Hand one trigger to the window; re-checked for the idle path.

        A poll skips its tick while the window is hidden, so a read
        already in flight when it went away must not start a scan
        either. The edge was consumed at observation, which is also what
        keeps a latch from firing once the window comes back.
        """
        if not self._stopped and not self._paused:
            context = self._context()
            if context.start_allowed and context.visible:
                self._on_trigger(trigger)
        return bool(GLib.SOURCE_REMOVE)

    def _stop_sensor_polling(self) -> None:
        """Disarm the poller and forget the arming state.

        Whatever latches while polling is stopped (a press during a
        scan) is baseline state when polling resumes, never a trigger.
        """
        if self._sensor_poll_id is not None:
            GLib.source_remove(self._sensor_poll_id)
            self._sensor_poll_id = None
        self._arbiter.reset()
