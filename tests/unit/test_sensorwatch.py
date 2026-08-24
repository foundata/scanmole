"""Tests for the idle sensor gate and arming rules (no GTK, no hardware).

``AdvisoryGate`` serializes access and ``SensorArbiter`` decides what an
observation means. What a window then does with a trigger it is handed,
and when it polls at all, is pinned in ``test_gui_sensor_polling.py``.
"""

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


# ---- the arbiter reading real capability evidence -------------------------


def test_probe_evidence_feeds_the_arbiter_without_a_synthetic_edge() -> None:
    # The whole chain, GTK-free: capability flow to arbiter. Device
    # selection probes bare and then source-applied; with paper already
    # loaded those two listings disagree only because they describe
    # different sources. Nothing may trigger from that, while a genuine
    # transition seen later by the idle poller still must.
    from scanmole.options import Capability
    from scanmole.sensors import assess_sensors
    from scanmole_gui.probing import CapabilityFlow

    def caps(page_loaded: str) -> dict[str, Capability]:
        return {
            "source": Capability(kind="enum", choices=["ADF Duplex", "Flatbed"]),
            "page-loaded": Capability(kind="bool", current=page_loaded),
        }

    flow = CapabilityFlow(preferred_source="adf-duplex")
    arbiter = SensorArbiter()
    observed: list[Observation] = []

    def feed(update: Any) -> None:
        if update.sensor_caps is not None:
            observed.append(arbiter.observe(assess_sensors(update.sensor_caps)))

    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe
    bare = flow.probe_completed(token, request, caps("no"), "dev-a", "adf-duplex")
    feed(bare)
    assert bare.start_probe is not None
    adf_token, adf_request = bare.start_probe
    applied = flow.probe_completed(
        adf_token, adf_request, caps("yes"), "dev-a", "adf-duplex"
    )
    feed(applied)

    # One observation only (the selected source's), and a baseline never
    # triggers, so the loaded sheet stays state rather than a request.
    assert observed == [Observation()]

    # A genuine transition afterwards still arms exactly once.
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_PAPER) == Observation(insert=True)
    assert arbiter.observe(_PAPER) == Observation()
