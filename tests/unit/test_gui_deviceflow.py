"""Tests for the GUI's device-coordination lifecycle owner (no hardware).

DeviceFlow composes the pure policy modules (discovery, probing,
advisory, sensorwatch) with worker threads and main-loop timers; these
tests replace the loop with a deterministic fake and hold the workers,
so every interleaving is driven explicitly. The policies themselves are
pinned in their own modules and are not re-proven here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from typing import Any

import pytest

from scanmole.options import Capability
from scanmole.sensors import SensorSnapshot

pytestmark = [
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
]

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.deviceflow imports GLib)",
)

_IDLE = SensorSnapshot(scan=False, page_loaded=False)
_BUTTON = SensorSnapshot(scan=True, page_loaded=False)
_PAPER = SensorSnapshot(scan=False, page_loaded=True)
_BOTH = SensorSnapshot(scan=True, page_loaded=True)

_DEVICE = "epsonds:net:host"
_OTHER = "fujitsu:ScanSnap iX100:X"


class _FakeGLib:
    """Deterministic GLib stand-in: sources fire only when told to."""

    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.timeouts: dict[int, tuple[int, Any]] = {}
        self.idles: list[tuple[Any, tuple[Any, ...]]] = []
        self.removed: list[str] = []
        self._next = 1

    def timeout_add(self, ms: int, callback: Any) -> int:
        self.timeouts[self._next] = (ms, callback)
        self._next += 1
        return self._next - 1

    def timeout_add_seconds(self, seconds: int, callback: Any) -> int:
        return self.timeout_add(seconds * 1000, callback)

    def idle_add(self, callback: Any, *args: object) -> int:
        self.idles.append((callback, args))
        return 0

    def source_remove(self, source: int) -> None:
        self.removed.append(f"remove:{source}")
        self.timeouts.pop(source, None)

    def drain_idles(self) -> None:
        """Run every queued idle callback once, in order."""
        pending, self.idles = self.idles, []
        for callback, args in pending:
            callback(*args)

    def timeout_intervals(self) -> list[int]:
        return [ms for ms, _cb in self.timeouts.values()]

    def poll_intervals(self) -> list[int]:
        """The armed device-poll intervals (the sensor poll is 2500 ms)."""
        return [ms for ms in self.timeout_intervals() if ms >= 15_000]

    def fire_device_poll(self) -> None:
        """Fire the single armed device-poll timeout."""
        polls = [
            (source, cb) for source, (ms, cb) in self.timeouts.items() if ms >= 15_000
        ]
        assert len(polls) == 1
        source, callback = polls[0]
        self.timeouts.pop(source)
        callback()


class _FakeAdvisory:
    """Holds spawned workers so the test drives every completion."""

    def __init__(self) -> None:
        self.generation = 0
        self.spawned: list[tuple[Any, tuple[Any, ...]]] = []
        self.cancels: list[bool] = []
        self.idle = True
        self.order: list[str] | None = None

    def spawn_worker(self, target: Any, *args: object) -> None:
        self.spawned.append((target, args))

    def cancel_pending(self, *, close: bool = False) -> bool:
        self.cancels.append(close)
        if self.order is not None:
            self.order.append("cancel")
        self.generation += 1
        return self.idle

    def adopter(self, generation: int) -> Any:
        return lambda _process: None


class _Result:
    """A ``run_command`` result double."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _snapshot(*, scan: str = "no", paper: str = "no") -> dict[str, Capability]:
    """A duplex feeder with a scan button and a paper level."""
    return {
        "source": Capability(
            kind="enum", choices=["ADF Front", "ADF Duplex"], current="ADF Duplex"
        ),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray", "Color"]),
        "scan": Capability(kind="bool", current=scan),
        "page-loaded": Capability(kind="bool", current=paper),
    }


def _listing_stdout(devices: list[dict[str, str]]) -> str:
    """A compatible ``--list-devices --json`` stream for these devices."""
    from scanmole_gui import __version__

    return (
        json.dumps({"event": "hello", "version": __version__})
        + "\n"
        + json.dumps({"event": "devices", "devices": devices})
        + "\n"
    )


class _Harness:
    """One DeviceFlow with a fake main loop, held workers and context."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, device: str | None = _DEVICE
    ) -> None:
        from scanmole_gui import deviceflow

        self.module = deviceflow
        self.glib = _FakeGLib()
        monkeypatch.setattr(deviceflow, "GLib", self.glib)
        self.device = device
        self.remembered = ""
        self.source = "adf-duplex"
        self.prefs: tuple[str, bool] = ("same", False)
        self.runner_free = True
        self.visible = True
        self.suspended = False
        self.searches = 0
        self.listings: list[Any] = []
        self.updates: list[Any] = []
        self.triggers: list[Any] = []
        self.logs: list[str] = []
        self.flow = deviceflow.DeviceFlow(
            scanmole="scanmole",
            context=self._context,
            on_searching=self._on_searching,
            on_listing=self.listings.append,
            on_capabilities=self.updates.append,
            on_trigger=self.triggers.append,
            on_log=self.logs.append,
        )
        self.advisory = _FakeAdvisory()
        # Held workers, driven by the test.
        self.flow._advisory = self.advisory  # type: ignore[assignment]

    def _on_searching(self) -> None:
        self.searches += 1

    def _context(self) -> Any:
        # Mirrors the window's wiring: Start needs an idle runner, a
        # driveable CLI, no active search and a selected device.
        allowed = (
            self.runner_free
            and not self.flow.cli_blocked
            and not self.flow.searching
            and self.device is not None
        )
        return self.module.DeviceContext(
            selected_device=self.device,
            remembered_device=self.remembered,
            source=self.source,
            sensor_prefs=self.prefs,
            start_allowed=allowed,
            visible=self.visible,
            suspended=self.suspended,
        )

    def run_search(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        devices: list[dict[str, str]] | None = None,
        effect: Any = None,
    ) -> None:
        """Run the held discovery worker inline and apply its result."""
        listing = _listing_stdout(devices if devices is not None else [])

        def fake_run(argv: list[str], **_kw: object) -> _Result:
            if "--version" in argv:
                return _Result(stdout="scanmole 9.9.9\n")
            if effect is not None:
                return effect(argv)  # type: ignore[no-any-return]
            return _Result(stdout=listing)

        monkeypatch.setattr(self.module, "run_command", fake_run)
        target, args = self.advisory.spawned.pop()
        assert target == self.flow._devices_worker
        target(*args)
        self.glib.drain_idles()

    def complete_probe(self, snapshot: object) -> None:
        """Feed a snapshot back for the most recently spawned probe."""
        target, (token, request, generation) = self.advisory.spawned.pop()
        assert target == self.flow._probe_worker
        self.flow._probe_done(token, request, snapshot, generation)

    def settle_negotiation(self, snapshot: dict[str, Capability]) -> None:
        """Complete the bare probe and its source-applied follow-up."""
        self.complete_probe(snapshot)  # bare: derives source availability
        self.complete_probe(snapshot)  # refinement under the selected source

    def seed_devices(
        self, monkeypatch: pytest.MonkeyPatch, devices: list[dict[str, str]]
    ) -> None:
        """One completed non-quiet search, negotiation settled."""
        self.flow.start()
        self.run_search(monkeypatch, devices=devices)
        if devices:
            self.settle_negotiation(_snapshot())
        self.listings.clear()
        self.updates.clear()
        self.logs.clear()
        self.searches = 0


_ENTRY = {"device": _DEVICE, "vendor": "EPSON", "model": "DS"}
_OTHER_ENTRY = {"device": _OTHER, "vendor": "FUJITSU", "model": "iX100"}


# ------------------------------------------------------------- discovery


@_NEEDS_GI
def test_initial_discovery_lists_devices_and_negotiates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.start()

    assert harness.searches == 1  # a non-quiet search paints its start
    searching_during = harness.flow.searching
    assert searching_during is True
    harness.run_search(monkeypatch, devices=[_ENTRY])

    searching_after = harness.flow.searching
    assert searching_after is False
    (outcome,) = harness.listings
    assert [d["device"] for d in outcome.devices] == [_DEVICE]
    assert outcome.unchanged is False
    assert outcome.failure is None
    assert outcome.poll_scheduled is True
    # The listing applied: a bare capability probe of the selection runs.
    target, (_token, request, _gen) = harness.advisory.spawned[-1]
    assert target == harness.flow._probe_worker
    assert request.device == _DEVICE and request.settings == ()
    # A populated list is presence-checked at the slow cadence.
    assert harness.glib.poll_intervals() == [45_000]


@_NEEDS_GI
def test_a_second_refresh_waits_for_the_running_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.start()
    harness.flow.refresh()

    assert harness.searches == 1  # no double search, no double paint
    assert len(harness.advisory.spawned) == 1


@_NEEDS_GI
def test_a_quiet_unchanged_result_renders_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    probes = len(harness.advisory.spawned)

    harness.flow.refresh(quiet=True)
    assert harness.searches == 0  # quiet: nothing painted at start
    harness.run_search(monkeypatch, devices=[_ENTRY])

    (outcome,) = harness.listings
    assert outcome.unchanged is True
    assert len(harness.advisory.spawned) == probes  # no renegotiation
    assert harness.glib.poll_intervals() == [45_000]


@_NEEDS_GI
def test_a_changed_quiet_result_applies_fully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])

    harness.flow.refresh(quiet=True)
    harness.run_search(monkeypatch, devices=[_ENTRY, _OTHER_ENTRY])

    (outcome,) = harness.listings
    assert outcome.unchanged is False
    assert len(outcome.devices) == 2
    # The new listing negotiates again; the cached snapshots answer
    # instantly, so availability renders without a fresh probe.
    assert harness.updates


@_NEEDS_GI
def test_presence_intervals_follow_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _Harness(monkeypatch, device=None)
    empty.flow.start()
    empty.run_search(monkeypatch, devices=[])
    assert empty.glib.poll_intervals() == [15_000]  # fast pickup

    found = _Harness(monkeypatch)
    found.seed_devices(monkeypatch, [_ENTRY])
    assert found.glib.poll_intervals() == [45_000]  # slow presence check


@_NEEDS_GI
def test_the_poll_defers_while_paused_searching_or_suspended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for prepare in (
        lambda h: h.flow.pause_for_scan(),
        lambda h: setattr(h.flow, "_searching", True),
        lambda h: setattr(h, "suspended", True),
    ):
        harness = _Harness(monkeypatch)
        harness.seed_devices(monkeypatch, [_ENTRY])
        spawned = len(harness.advisory.spawned)
        prepare(harness)

        harness.glib.fire_device_poll()

        # Deferred a full interval: no search started, the timer re-armed.
        assert len(harness.advisory.spawned) == spawned
        assert harness.glib.poll_intervals() == [45_000]


@_NEEDS_GI
def test_the_poll_stops_for_an_incompatible_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.start()
    harness.run_search(
        monkeypatch,
        effect=lambda _argv: _Result(
            stdout=json.dumps({"event": "hello", "version": "99.0.0"}) + "\n"
        ),
    )

    (outcome,) = harness.listings
    assert outcome.failure is harness.module.DiscoveryFailure.INCOMPATIBLE_CLI
    assert outcome.needed is not None
    assert harness.flow.cli_blocked is True
    assert outcome.poll_scheduled is False
    assert harness.glib.timeouts == {}  # no automatic retry against it


@_NEEDS_GI
def test_a_vanished_selected_device_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])

    harness.flow.refresh(quiet=True)
    harness.run_search(monkeypatch, devices=[])

    (outcome,) = harness.listings
    assert outcome.vanished is True
    assert outcome.prefer == _DEVICE  # the reappearance reselects it
    assert harness.glib.poll_intervals() == [15_000]  # fast pickup again


@_NEEDS_GI
@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (FileNotFoundError("scanmole"), "CLI_MISSING"),
        (subprocess.TimeoutExpired(cmd="scanmole", timeout=120), "TIMED_OUT"),
        (OSError("bad interpreter"), "OS_ERROR"),
        (RuntimeError("boom"), "UNEXPECTED"),
    ],
)
def test_discovery_failure_kinds_are_typed(
    monkeypatch: pytest.MonkeyPatch, effect: Exception, expected: str
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.start()

    def raising(_argv: list[str]) -> _Result:
        raise effect

    harness.run_search(monkeypatch, effect=raising)

    (outcome,) = harness.listings
    assert outcome.failure is harness.module.DiscoveryFailure[expected]
    if expected == "OS_ERROR":
        assert outcome.error_detail == "bad interpreter"
    assert harness.flow.searching is False  # the latch never sticks
    assert harness.glib.poll_intervals() == [15_000]  # retry armed


@_NEEDS_GI
def test_a_failed_exit_carries_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.start()
    harness.run_search(
        monkeypatch, effect=lambda _argv: _Result(stdout="", returncode=3)
    )

    (outcome,) = harness.listings
    assert outcome.failure is harness.module.DiscoveryFailure.FAILED_EXIT
    assert outcome.failed_exit == 3


@_NEEDS_GI
def test_a_stale_search_result_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The invalidating owner (takeover, stop, a newer search) already
    # cleaned up its own state; a stale completion must not touch the
    # latch a newer owner may hold, render anything or arm a timer.
    harness = _Harness(monkeypatch)
    harness.flow.start()
    harness.advisory.generation += 1  # cancelled underneath

    harness.run_search(monkeypatch, devices=[_ENTRY])

    assert harness.listings == []  # nothing renders
    assert harness.flow.searching is True  # the latch is not its to clear
    assert harness.glib.timeouts == {}  # and no poll chain was armed


@_NEEDS_GI
def test_the_discovery_worker_mutates_no_controller_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker returns typed data only. A cancelled worker resuming
    # past the takeover must not overwrite live compatibility state from
    # its thread, however its listing came out.
    harness = _Harness(monkeypatch)
    harness.flow.start()
    target, args = harness.advisory.spawned.pop()
    harness.advisory.generation += 1  # the takeover happened in between

    def incompatible(argv: list[str], **_kw: object) -> _Result:
        if "--version" in argv:
            return _Result(stdout="scanmole 99.0.0\n")
        return _Result(
            stdout=json.dumps({"event": "hello", "version": "99.0.0"}) + "\n",
            stderr="ancient noise\n",
        )

    monkeypatch.setattr(harness.module, "run_command", incompatible)
    target(*args)  # the worker body itself, before any main-loop apply

    assert harness.flow.cli_blocked is False  # untouched from the thread
    assert harness.flow.cli_version is None
    harness.glib.drain_idles()  # the stale apply and stale stderr log
    assert harness.flow.cli_blocked is False  # and untouched by the apply
    assert harness.flow.cli_version is None
    assert harness.logs == []  # not even the stderr line renders


@_NEEDS_GI
def test_a_cancelled_search_cannot_poison_its_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The interleaving: search A starts, a scan takeover cancels it,
    # coordination resumes and search B starts; A then completes with an
    # incompatible result. B must stay searching with its compatibility
    # state untouched, and B's own completion is the only one applied.
    harness = _Harness(monkeypatch)
    harness.flow.start()  # search A
    a_target, a_args = harness.advisory.spawned.pop()

    harness.flow.pause_for_scan()  # the takeover cancels A
    harness.flow.resume_after_scan()
    harness.advisory.spawned.clear()  # drop the resume's probe worker
    harness.flow.refresh()  # search B
    b_target, b_args = harness.advisory.spawned.pop()
    searching_before = harness.flow.searching
    assert searching_before is True

    def incompatible(argv: list[str], **_kw: object) -> _Result:
        if "--version" in argv:
            return _Result(stdout="scanmole 99.0.0\n")
        return _Result(
            stdout=json.dumps({"event": "hello", "version": "99.0.0"}) + "\n"
        )

    monkeypatch.setattr(harness.module, "run_command", incompatible)
    a_target(*a_args)
    harness.glib.drain_idles()

    searching_after_a = harness.flow.searching
    assert searching_after_a is True  # B keeps its latch
    assert harness.flow.cli_blocked is False  # and its compatibility state
    assert harness.flow.cli_version is None
    assert harness.listings == []

    def compatible(argv: list[str], **_kw: object) -> _Result:
        if "--version" in argv:
            return _Result(stdout="scanmole 9.9.9\n")
        return _Result(stdout=_listing_stdout([_ENTRY]))

    monkeypatch.setattr(harness.module, "run_command", compatible)
    b_target(*b_args)
    harness.glib.drain_idles()

    searching_after_b = harness.flow.searching
    assert searching_after_b is False  # B's own completion applied
    assert harness.flow.cli_blocked is False
    (outcome,) = harness.listings
    assert [d["device"] for d in outcome.devices] == [_DEVICE]


# -------------------------------------------- capability negotiation


@_NEEDS_GI
def test_bare_probe_then_source_applied_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.device_changed()

    _target, (_t1, bare, _g1) = harness.advisory.spawned[-1]
    assert bare.settings == ()
    harness.complete_probe(_snapshot())
    _target, (_t2, follow, _g2) = harness.advisory.spawned[-1]
    assert follow.settings == (("--source", "ADF Duplex"),)
    harness.complete_probe(_snapshot())

    assert harness.flow.last_caps is not None
    assert harness.flow._flow.sensor_settings(_DEVICE, "adf-duplex") == (
        ("--source", "ADF Duplex"),
    )
    assert harness.updates  # availability rendered along the way


@_NEEDS_GI
def test_a_late_generation_probe_result_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow.device_changed()
    _target, (token, request, generation) = harness.advisory.spawned.pop()
    harness.advisory.generation += 1  # a takeover happened in between
    harness.updates.clear()

    harness.flow._probe_done(token, request, _snapshot(), generation)

    assert harness.updates == []  # the cancelled result must not render


@_NEEDS_GI
def test_settings_reset_renegotiates_without_rebaselining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    # Baseline is established; a reset must not turn the next real edge
    # into a discarded baseline observation.
    harness.flow._sensor_done(_IDLE, harness.advisory.generation)
    harness.triggers.clear()

    harness.flow.settings_reset()

    assert harness.updates  # renegotiated (instantly, from the cache)
    harness.flow._sensor_done(_BUTTON, harness.advisory.generation)
    assert len(harness.triggers) == 1  # the edge survived the reset


# ------------------------------------------------------- scan takeover


@_NEEDS_GI
def test_takeover_stops_polling_then_cancels_then_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    assert harness.glib.timeouts  # device poll armed; arm the sensor one too
    harness.flow._schedule_sensor_poll()

    order: list[str] = []
    harness.advisory.order = order
    real_stop = harness.flow._stop_sensor_polling
    real_reset = harness.flow._flow.reset

    def stop_sensors() -> None:
        order.append("stop-sensors")
        real_stop()

    def flow_reset() -> None:
        order.append("flow-reset")
        real_reset()

    monkeypatch.setattr(harness.flow, "_stop_sensor_polling", stop_sensors)
    monkeypatch.setattr(harness.flow._flow, "reset", flow_reset)
    harness.flow._searching = True  # a search was running

    idle = harness.flow.pause_for_scan()

    assert order == ["stop-sensors", "cancel", "flow-reset"]
    assert idle is True
    assert harness.flow.searching is False  # the latch never survives
    assert harness.flow._flow.probe_active is False  # no phantom probe

    harness.advisory.idle = False  # a wedged worker reports through
    assert harness.flow.pause_for_scan() is False


@_NEEDS_GI
def test_resume_negotiates_before_polling_can_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The controller-level pin of the failed-start recovery: after the
    # takeover reset, retained caps must not rearm polling; the resume
    # probes first and polling waits for the accepted fresh evidence.
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    harness.flow.pause_for_scan()
    assert harness.flow.last_caps is not None  # retained across the reset

    harness.flow.resume_after_scan()

    assert harness.flow._flow.probe_active is True  # fresh bare probe
    target, (_t, request, _g) = harness.advisory.spawned[-1]
    assert target == harness.flow._probe_worker
    assert request.device == _DEVICE and request.settings == ()
    sensor_polls = [ms for ms in harness.glib.timeout_intervals() if ms == 2500]
    assert sensor_polls == []  # nothing armed from the retained caps

    harness.settle_negotiation(_snapshot())
    assert 2500 in harness.glib.timeout_intervals()  # armed again, fresh


@_NEEDS_GI
def test_a_paused_controller_starts_no_search_and_no_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    harness.flow.pause_for_scan()
    spawned = len(harness.advisory.spawned)

    harness.flow.refresh()
    harness.flow.device_changed()  # invalidates, but must not probe

    assert len(harness.advisory.spawned) == spawned
    assert harness.searches == 0


# ------------------------------------------------ idle sensor polling


def _caps_window(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prefs: tuple[str, bool],
    caps: dict[str, Capability] | None,
) -> Any:
    harness = _Harness(monkeypatch)
    harness.prefs = prefs
    harness.flow._flow.last_caps = caps
    return harness


@_NEEDS_GI
@pytest.mark.parametrize(
    ("mapping", "insert", "sensors", "wanted"),
    [
        # A preference is worth polling for only where the device has the
        # sensor that would answer it.
        ("same", False, ("scan",), True),
        ("single", False, ("scan",), True),
        ("collect", False, ("scan",), True),
        ("same", False, ("page-loaded",), False),
        ("same", False, (), False),
        ("same", True, ("page-loaded",), True),  # the insertion carries it
        ("off", True, ("page-loaded",), True),
        ("off", True, ("scan",), False),
        ("off", False, ("scan", "page-loaded"), False),
        ("same", False, ("scan", "page-loaded"), True),
    ],
)
def test_polling_needs_a_sensor_for_the_enabled_preference(
    monkeypatch: pytest.MonkeyPatch,
    mapping: str,
    insert: bool,
    sensors: tuple[str, ...],
    wanted: bool,
) -> None:
    harness = _caps_window(
        monkeypatch,
        prefs=(mapping, insert),
        caps={name: Capability(kind="bool", current="no") for name in sensors},
    )

    assert harness.flow._polling_wanted(harness._context()) is wanted


@_NEEDS_GI
def test_polling_wanted_requires_predicate_probe_idle_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _caps_window(
        monkeypatch,
        prefs=("same", False),
        caps={"scan": Capability(kind="bool", current="no")},
    )
    assert harness.flow._polling_wanted(harness._context()) is True

    harness.runner_free = False  # Start blocked (scan running, ...)
    assert harness.flow._polling_wanted(harness._context()) is False
    harness.runner_free = True

    harness.flow._flow._coordinator.begin(  # a capability probe is active
        harness.module.ProbeRequest(_DEVICE)
    )
    assert harness.flow._polling_wanted(harness._context()) is False
    harness.flow._flow._coordinator = type(harness.flow._flow._coordinator)()

    caps: dict[str, Capability] | None
    for caps in ({}, None):  # no usable evidence: never a device list
        harness.flow._flow.last_caps = caps
        assert harness.flow._polling_wanted(harness._context()) is False
    harness.flow._flow.last_caps = {"scan": Capability(kind="bool", current="no")}

    harness.flow.pause_for_scan()  # a scan owns the device
    assert harness.flow._polling_wanted(harness._context()) is False


@_NEEDS_GI
def test_inconclusive_sensor_evidence_never_enables_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sensor recognition is exact: an inactive option, a non-boolean one
    # or an unparseable value is unavailable evidence, not a scan button.
    for capability in (
        Capability(kind="bool", current="no", active=False),
        Capability(kind="enum", choices=["yes", "no"], current="no"),
        Capability(kind="bool", current="<yes|no>"),
    ):
        harness = _caps_window(
            monkeypatch, prefs=("same", False), caps={"scan": capability}
        )
        assert harness.flow._polling_wanted(harness._context()) is False


@_NEEDS_GI
def test_the_sensor_tick_reads_under_the_selected_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    assert 2500 in harness.glib.timeout_intervals()

    for source, (ms, callback) in list(harness.glib.timeouts.items()):
        if ms == 2500:
            harness.glib.timeouts.pop(source)
            callback()

    target, (device, settings, _gen) = harness.advisory.spawned[-1]
    assert target == harness.flow._sensor_worker
    assert device == _DEVICE
    assert settings == (("--source", "ADF Duplex"),)  # never the default


@_NEEDS_GI
@pytest.mark.parametrize(
    ("visible", "suspended", "reads"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),  # ordinarily hidden, not suspended
        (False, True, False),
    ],
)
def test_sensor_reads_need_a_visible_unsuspended_window(
    monkeypatch: pytest.MonkeyPatch, visible: bool, suspended: bool, reads: bool
) -> None:
    # Only a window the user can see, and that the compositor has not
    # suspended, may open the scanner for an idle sensor read; every
    # other state skips the tick and re-arms without a command.
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    harness.visible = visible
    harness.suspended = suspended
    spawned = len(harness.advisory.spawned)

    for source, (ms, callback) in list(harness.glib.timeouts.items()):
        if ms == 2500:
            harness.glib.timeouts.pop(source)
            callback()

    if reads:
        target, _args = harness.advisory.spawned[-1]
        assert target == harness.flow._sensor_worker
    else:
        assert len(harness.advisory.spawned) == spawned  # scanner untouched
        assert 2500 in harness.glib.timeout_intervals()  # tries again later


@_NEEDS_GI
@pytest.mark.parametrize(
    ("visible", "suspended", "searches"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),  # ordinarily hidden, not suspended
        (False, True, False),
    ],
)
def test_presence_polls_need_a_visible_unsuspended_window(
    monkeypatch: pytest.MonkeyPatch, visible: bool, suspended: bool, searches: bool
) -> None:
    # The quiet presence check opens no device, but it still runs a
    # discovery command; a hidden or suspended window defers the tick a
    # full interval instead, keeping the ordinary retry cadence.
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    harness.visible = visible
    harness.suspended = suspended
    spawned = len(harness.advisory.spawned)

    harness.glib.fire_device_poll()

    if searches:
        target, _args = harness.advisory.spawned[-1]
        assert target == harness.flow._devices_worker
    else:
        assert len(harness.advisory.spawned) == spawned  # no discovery ran
        assert harness.glib.poll_intervals() == [45_000]  # deferred, re-armed


@_NEEDS_GI
@pytest.mark.parametrize(
    ("visible", "suspended", "emitted"),
    [
        (True, False, True),
        (True, True, True),  # the window predicate declines it there
        (False, False, False),
        (False, True, False),
    ],
)
def test_trigger_emission_follows_visibility(
    monkeypatch: pytest.MonkeyPatch, visible: bool, suspended: bool, emitted: bool
) -> None:
    # Hidden consumes the edge in the controller; a merely suspended
    # window still receives the request and the window's authoritative
    # predicate (which rechecks both) declines it, consumed either way.
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)
    harness.visible = visible
    harness.suspended = suspended

    harness.flow._sensor_done(_BUTTON, 0)

    assert bool(harness.triggers) is emitted


@_NEEDS_GI
def test_showing_the_window_rebaselines_before_latches_can_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # While hidden the tick skips, so no read consumes a press made in
    # between; the show transition must re-baseline, or that stale latch
    # would start a scan the moment the window returns.
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)  # baseline while visible
    harness.visible = False
    harness.flow.view_state_changed()

    harness.visible = True
    harness.flow.view_state_changed()
    harness.flow._sensor_done(_BUTTON, 0)  # first read after showing

    assert harness.triggers == []  # a discarded baseline, never a trigger
    harness.flow._sensor_done(_IDLE, 0)
    harness.flow._sensor_done(_BUTTON, 0)
    assert len(harness.triggers) == 1  # a genuinely fresh edge still does


@_NEEDS_GI
def test_a_press_latched_across_suspension_never_triggers_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window stays visible while the compositor suspends it: no poll
    # runs in between, so a button latched during suspension is state,
    # not a request. Resuming must re-baseline before the next read.
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)  # baseline at no

    harness.suspended = True  # suspended, still visible
    harness.flow.view_state_changed()
    assert harness.triggers == []  # entering the state triggers nothing
    # ... the scanner latches yes; no poll runs while suspended ...
    harness.suspended = False  # resume
    harness.flow.view_state_changed()

    harness.flow._sensor_done(_BUTTON, 0)  # first read: the stale latch
    assert harness.triggers == []  # a discarded baseline, never a trigger
    harness.flow._sensor_done(_IDLE, 0)
    harness.flow._sensor_done(_BUTTON, 0)
    assert len(harness.triggers) == 1  # one genuine edge, exactly once


@_NEEDS_GI
def test_a_suspended_window_skips_the_tick_and_rearms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    harness.suspended = True
    spawned = len(harness.advisory.spawned)

    for source, (ms, callback) in list(harness.glib.timeouts.items()):
        if ms == 2500:
            harness.glib.timeouts.pop(source)
            callback()

    assert len(harness.advisory.spawned) == spawned  # no read while hidden
    assert 2500 in harness.glib.timeout_intervals()  # tries again later


@_NEEDS_GI
@pytest.mark.parametrize(
    ("mapping", "expected"),
    [("same", "same"), ("single", "single"), ("collect", "collect"), ("off", None)],
)
def test_a_button_edge_requests_its_mapping(
    monkeypatch: pytest.MonkeyPatch, mapping: str, expected: str | None
) -> None:
    harness = _Harness(monkeypatch)
    harness.prefs = (mapping, False)
    generation = harness.advisory.generation
    harness.flow._sensor_done(_IDLE, generation)  # baseline

    harness.flow._sensor_done(_BUTTON, generation)

    if expected is None:
        assert harness.triggers == []
    else:
        (trigger,) = harness.triggers
        assert trigger.mapping == expected
        assert trigger.reason == "hardware button"


@_NEEDS_GI
def test_an_insertion_requests_the_form_flow_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled = _Harness(monkeypatch)
    enabled.prefs = ("off", True)
    enabled.flow._sensor_done(_IDLE, 0)
    enabled.flow._sensor_done(_PAPER, 0)
    (trigger,) = enabled.triggers
    assert trigger.mapping == "same" and trigger.reason == "paper inserted"

    disabled = _Harness(monkeypatch)
    disabled.prefs = ("off", False)
    disabled.flow._sensor_done(_IDLE, 0)
    disabled.flow._sensor_done(_PAPER, 0)
    assert disabled.triggers == []


@_NEEDS_GI
def test_a_button_beats_an_insertion_in_the_same_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.prefs = ("single", True)
    harness.flow._sensor_done(_IDLE, 0)

    harness.flow._sensor_done(_BOTH, 0)

    (trigger,) = harness.triggers
    assert trigger.mapping == "single"  # the explicit mapping won


@_NEEDS_GI
def test_a_blocked_trigger_is_consumed_and_never_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)
    harness.runner_free = False

    harness.flow._sensor_done(_BUTTON, 0)
    assert harness.triggers == []

    # Once Start is allowed again, the consumed press must not replay;
    # only a genuinely new edge triggers.
    harness.runner_free = True
    harness.flow._sensor_done(_BUTTON, 0)  # still latched: no new edge
    assert harness.triggers == []
    harness.flow._sensor_done(_IDLE, 0)
    harness.flow._sensor_done(_BUTTON, 0)
    assert len(harness.triggers) == 1


@_NEEDS_GI
def test_a_hidden_window_consumes_a_trigger_instead_of_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ordinarily hidden window, not a compositor-suspended one: the
    # in-flight read finishes and its edge is consumed without a scan.
    harness = _Harness(monkeypatch)
    harness.visible = False
    harness.flow._sensor_done(_IDLE, 0)

    harness.flow._sensor_done(_BUTTON, 0)
    assert harness.triggers == []

    harness.visible = True
    harness.flow._sensor_done(_BUTTON, 0)  # still latched: no new edge
    assert harness.triggers == []
    harness.flow._sensor_done(_IDLE, 0)
    harness.flow._sensor_done(_BUTTON, 0)
    assert len(harness.triggers) == 1


@_NEEDS_GI
def test_a_stale_or_stopped_poll_result_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)

    harness.flow._sensor_done(_BUTTON, 7)  # cancelled underneath
    assert harness.triggers == []

    harness.flow.stop()
    harness.flow._sensor_done(_BUTTON, harness.advisory.generation)
    assert harness.triggers == []


@_NEEDS_GI
def test_an_open_failure_logs_once_and_hands_back_to_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    harness.flow._sensor_done(_IDLE, 0)

    harness.flow._sensor_done(None, 0)

    outage_lines = [line for line in harness.logs if "sensor" in line]
    assert len(outage_lines) == 1  # one line per outage, no error storm
    assert harness.searches == 1  # ordinary (painted) discovery takes over
    assert harness.flow.searching is True

    # A second failure while that search still runs adds nothing.
    harness.flow._sensor_done(None, harness.advisory.generation)
    assert len([line for line in harness.logs if "sensor" in line]) == 1
    assert harness.searches == 1


@_NEEDS_GI
def test_accepted_probe_evidence_defers_its_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live source-matched probe consumed the device's latch, so its
    # evidence feeds the arbiter, but the trigger fires from an idle
    # callback: the flow bookkeeping this result belongs to must finish
    # first. A press during the follow-up probe is exactly this case.
    harness = _Harness(monkeypatch)
    harness.flow.device_changed()
    harness.complete_probe(_snapshot())  # bare
    harness.complete_probe(_snapshot())  # follow-up: baseline observation
    harness.flow._sensor_done(_IDLE, harness.advisory.generation)

    harness.flow.source_changed(manual=False)
    # Reconciliation kept the source; no new probe request was derived.
    harness.flow.device_changed()  # the same selection probes from cache
    assert harness.triggers == []

    # A genuinely fresh live read arrives with the button latched.
    harness.flow._flow.reset()  # cache gone (as after a takeover)
    harness.flow.device_changed()
    harness.complete_probe(_snapshot())  # bare again
    harness.flow._sensor_done(_IDLE, harness.advisory.generation)  # baseline
    harness.complete_probe(_snapshot(scan="yes"))  # live follow-up, pressed

    assert harness.triggers == []  # not inline
    harness.glib.drain_idles()
    (trigger,) = harness.triggers
    assert trigger.reason == "hardware button"


# ------------------------------------------------------ stop and after


@_NEEDS_GI
def test_stop_is_idempotent_and_final(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(monkeypatch)
    harness.seed_devices(monkeypatch, [_ENTRY])
    assert harness.glib.timeouts  # timers armed before the stop

    harness.flow.stop()
    harness.flow.stop()  # idempotent

    assert harness.advisory.cancels == [True, True]  # closed for good
    assert harness.glib.timeouts == {}  # no timer left to fire

    # No late result of any kind reaches the window afterwards.
    harness.flow._apply_listing(
        harness.module._SearchResult(devices=[_ENTRY]),
        _DEVICE,
        harness.advisory.generation,
        harness.flow._search_token,
        False,
    )
    harness.flow._probe_done(1, harness.module.ProbeRequest(_DEVICE), _snapshot(), 99)
    harness.flow._sensor_done(_BUTTON, harness.advisory.generation)
    harness.flow._log_line(
        "[gui] late worker noise",
        harness.advisory.generation,
        harness.flow._search_token,
    )
    harness.flow.refresh()
    harness.flow.device_changed()
    harness.flow.resume_after_scan()

    assert harness.listings == []
    assert harness.updates == []
    assert harness.triggers == []
    assert harness.logs == []
    assert harness.searches == 0
