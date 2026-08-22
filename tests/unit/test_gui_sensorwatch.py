"""Tests for the idle sensor gate and arming rules (no GTK, no hardware)."""

from __future__ import annotations

import threading
from typing import Any

from scanmole.sensors import SensorSnapshot
from scanmole_gui.sensorwatch import AdvisoryGate, Observation, SensorArbiter

_IDLE = SensorSnapshot(scan=False, page_loaded=False)
_BUTTON = SensorSnapshot(scan=True, page_loaded=False)
_PAPER = SensorSnapshot(scan=False, page_loaded=True)
_BOTH = SensorSnapshot(scan=True, page_loaded=True)


def test_gate_serializes_sensor_and_priority_access() -> None:
    gate = AdvisoryGate()

    assert gate.try_acquire("sensor")
    assert not gate.try_acquire("sensor")  # one at a time
    assert not gate.acquire("probe", timeout=0.05)  # held: priority times out
    gate.release()
    assert gate.acquire("probe", timeout=0.05)
    assert not gate.try_acquire("sensor")  # priority work owns the device
    gate.release()
    assert gate.try_acquire("sensor")


def test_gate_priority_waiters_starve_sensor_polls() -> None:
    # While discovery or a probe waits for the gate, a sensor poll must
    # not sneak in ahead of it after the release.
    gate = AdvisoryGate()
    assert gate.try_acquire("sensor")
    got_priority = threading.Event()

    def priority() -> None:
        assert gate.acquire("discovery", timeout=5.0)
        got_priority.set()

    waiter = threading.Thread(target=priority)
    waiter.start()
    for _ in range(100):
        if not gate.try_acquire("sensor"):
            break
    assert not gate.try_acquire("sensor")  # refused while priority waits
    gate.release()
    assert got_priority.wait(5.0)
    gate.release()
    waiter.join(5.0)
    assert gate.try_acquire("sensor")


def test_the_first_observation_is_a_baseline_and_never_triggers() -> None:
    arbiter = SensorArbiter()

    assert arbiter.observe(_BOTH) == Observation()  # latched press + loaded sheet


def test_a_fresh_button_edge_triggers_once() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_BUTTON) == Observation(button=True)
    # A backend keeping the value latched across reads yields no second
    # trigger: one edge, one trigger.
    assert arbiter.observe(_BUTTON) == Observation()
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_BUTTON) == Observation(button=True)


def test_insert_needs_a_real_no_to_yes_transition() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_PAPER) == Observation(insert=True)
    assert arbiter.observe(_PAPER) == Observation()  # level, not a new edge
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_PAPER) == Observation(insert=True)


def test_paper_needs_an_observed_empty_level_to_be_an_insertion() -> None:
    # Without one, a yes could just as well be a sheet that was lying
    # there all along.
    arbiter = SensorArbiter()
    arbiter.observe(SensorSnapshot(scan=False, page_loaded=None))

    assert arbiter.observe(_PAPER) == Observation()


def test_unavailable_reads_do_not_break_an_insertion_chain() -> None:
    # Empty, then a poll the sensor did not answer, then paper: the sheet
    # went in between those reads, whatever the gap. Forgetting the empty
    # level here would drop a real insertion for no safety gain.
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)
    arbiter.observe(SensorSnapshot(scan=False, page_loaded=None))

    assert arbiter.observe(_PAPER) == Observation(insert=True)


def test_button_and_insertion_in_one_observation_report_both() -> None:
    # The caller resolves the priority (an explicit button mapping wins);
    # the arbiter reports every fresh edge it saw.
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_BOTH) == Observation(button=True, insert=True)


def test_reset_makes_the_next_observation_a_baseline() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)
    arbiter.reset()

    # A press latched while a scan ran resumes as baseline state.
    assert arbiter.observe(_BUTTON) == Observation()


def test_offline_logs_once_per_outage_and_drops_the_baseline() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.mark_offline() is True
    assert arbiter.mark_offline() is False  # no per-second error storm
    # Recovery re-baselines: stale latches from the outage never trigger.
    assert arbiter.observe(_BUTTON) == Observation()
    assert arbiter.mark_offline() is True  # a new outage logs again


# ---- window-level trigger mapping (duck-typed, needs the app module) ------

import importlib.util  # noqa: E402

import pytest  # noqa: E402

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)


def _duck_window(
    *,
    mapping: str = "off",
    insert: bool = False,
    allowed: bool = True,
) -> Any:
    from scanmole_gui.app import MainWindow

    class Window:
        _observe_sensors = MainWindow._observe_sensors
        _sensor_prefs = MainWindow._sensor_prefs
        _sensor_trigger_allowed = MainWindow._sensor_trigger_allowed
        _window_suspended = MainWindow._window_suspended
        _trigger_sensor_scan = MainWindow._trigger_sensor_scan
        _on_sensor_poll_done = MainWindow._on_sensor_poll_done

        def __init__(self) -> None:
            self._settings = {"hardware_button": mapping, "insert_to_scan": insert}
            self._sensor_arbiter = SensorArbiter()
            self._sensor_poll_busy = False
            self._released = False
            self._searching = False
            self._runner = None
            self._advisory = type("A", (), {"generation": 0})()
            self.allowed = allowed
            self.started: list[str] = []
            self.logs: list[str] = []
            self.refreshes = 0
            self.scheduled = 0

            class Form:
                @staticmethod
                def sheet_flow_value() -> str:
                    return "collect"  # the persisted choice: collect is on

            self._form = Form()

        def _scan_allowed(self) -> bool:
            return self.allowed

        def _on_scan_clicked(self, flow: str = "stack") -> None:
            self.started.append(flow)

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _refresh_devices(self) -> None:
            self.refreshes += 1

        def _schedule_sensor_poll(self) -> None:
            self.scheduled += 1

    return Window()


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ("same", ["collect"]),
        ("single", ["single"]),
        ("collect", ["collect"]),
        ("off", []),
    ],
)
def test_button_mapping_selects_the_flow(mapping: str, expected: list[str]) -> None:
    window = _duck_window(mapping=mapping)
    window._observe_sensors(_IDLE)  # baseline

    window._observe_sensors(_BUTTON)

    assert window.started == expected


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_insertion_starts_the_persisted_flow_only_when_enabled() -> None:
    enabled = _duck_window(insert=True)
    enabled._observe_sensors(_IDLE)
    enabled._observe_sensors(_PAPER)
    assert enabled.started == ["collect"]

    disabled = _duck_window(insert=False)
    disabled._observe_sensors(_IDLE)
    disabled._observe_sensors(_PAPER)
    assert disabled.started == []


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_button_beats_an_insertion_in_the_same_observation() -> None:
    window = _duck_window(mapping="single", insert=True)
    window._observe_sensors(_IDLE)

    window._observe_sensors(_BOTH)

    assert window.started == ["single"]  # the explicit mapping won


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_blocked_trigger_is_consumed_and_never_queued() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)
    window.allowed = False

    window._observe_sensors(_BUTTON)
    assert window.started == []

    # Once Start is allowed again, the consumed press must not replay;
    # only a genuinely new edge triggers.
    window.allowed = True
    window._observe_sensors(_BUTTON)  # still latched: no new edge
    assert window.started == []
    window._observe_sensors(_IDLE)
    window._observe_sensors(_BUTTON)
    assert window.started == ["collect"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_an_open_failure_requests_discovery_and_logs_once() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)

    window._on_sensor_poll_done(None, 0)
    window._on_sensor_poll_done(None, 0)

    assert window.refreshes == 2  # discovery takes over
    outage_lines = [line for line in window.logs if "sensor" in line]
    assert len(outage_lines) == 1  # one line per outage, no error storm
    assert window.scheduled == 0  # polling stopped until discovery decides


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_stale_or_released_poll_result_is_dropped() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)

    window._advisory.generation = 7  # cancelled underneath
    window._on_sensor_poll_done(_BUTTON, 0)
    assert window.started == []
    assert window.scheduled == 0

    window._advisory.generation = 0
    window._released = True
    window._on_sensor_poll_done(_BUTTON, 0)
    assert window.started == []


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_only_flow_accepted_probe_evidence_feeds_the_arbiter() -> None:
    # A live capability probe read the device and consumed any latch, but
    # only the flow knows whether that read still describes the current
    # selection. A result it rejects (another device, a source the user
    # has left) must not arm anything, and a cached snapshot never
    # reaches this path at all.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.probing import CapabilityUpdate, ProbeRequest

    order: list[str] = []
    outcome: list[CapabilityUpdate] = []

    class Window:
        _on_probe_done = MainWindow._on_probe_done

        def __init__(self) -> None:
            self._released = False
            self._advisory = type("A", (), {"generation": 0})()
            self.observed: list[object] = []

            class Flow:
                @staticmethod
                def probe_completed(*args: object) -> object:
                    order.append("flow")
                    return outcome[0]

            self._flow = Flow()

        def _selected_device(self) -> str:
            return "sane:0"

        class _Form:
            @staticmethod
            def source_value() -> str:
                return "adf"

        _form = _Form()

        def _observe_sensors(
            self, snapshot: object, *, defer_trigger: bool = False
        ) -> None:
            order.append("observe")
            self.observed.append((snapshot, defer_trigger))

        def _render_capability_update(self, update: object) -> None:
            order.append("render")

    window: Any = Window()
    outcome.append(CapabilityUpdate())  # the flow rejected the result
    window._on_probe_done(1, ProbeRequest("sane:0", ()), {}, 0)
    assert window.observed == []
    assert order == ["flow", "render"]

    order.clear()
    outcome[0] = CapabilityUpdate(sensor_caps={})
    window._on_probe_done(1, ProbeRequest("sane:0", ()), {}, 0)
    assert order == ["flow", "observe", "render"]  # accepted, before rendering
    assert window.observed[0][1] is True  # the trigger is deferred


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_hidden_window_consumes_a_trigger_instead_of_scanning() -> None:
    # Polls skip their tick while the window is hidden, so a read already
    # in flight when it went away must not start a scan either. The edge
    # is consumed, which is what keeps it from firing on return.
    window = _duck_window(mapping="same")
    window.is_suspended = lambda: True
    window._observe_sensors(_IDLE)

    window._observe_sensors(_BUTTON)
    assert window.started == []

    window.is_suspended = lambda: False
    window._observe_sensors(_BUTTON)  # still latched: no new edge
    assert window.started == []
    window._observe_sensors(_IDLE)
    window._observe_sensors(_BUTTON)
    assert window.started == ["collect"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_polling_wanted_requires_prefs_predicate_probe_idle_and_sensors() -> None:
    from scanmole.options import Capability
    from scanmole_gui.app import MainWindow

    class Window:
        _sensor_polling_wanted = MainWindow._sensor_polling_wanted
        _sensor_prefs = MainWindow._sensor_prefs

        def __init__(self) -> None:
            self._settings: dict[str, object] = {"hardware_button": "same"}
            self._released = False
            self._closing = False
            self.allowed = True

            class Flow:
                def __init__(self) -> None:
                    self.probe_active = False
                    self.last_caps: dict[str, object] | None = {
                        "scan": Capability(kind="bool", current="no")
                    }

            self._flow = Flow()

        def _scan_allowed(self) -> bool:
            return self.allowed

    window: Any = Window()
    assert window._sensor_polling_wanted() is True

    window._settings = {}  # every trigger off: no polling at all
    assert window._sensor_polling_wanted() is False
    window._settings = {"insert_to_scan": True}
    assert window._sensor_polling_wanted() is True

    window.allowed = False  # scan running, searching, no device, blocked CLI
    assert window._sensor_polling_wanted() is False
    window.allowed = True

    window._flow.probe_active = True  # a capability probe owns the device
    assert window._sensor_polling_wanted() is False
    window._flow.probe_active = False

    window._flow.last_caps = {}  # no usable sensors: never a device list
    assert window._sensor_polling_wanted() is False
    window._flow.last_caps = None
    assert window._sensor_polling_wanted() is False
