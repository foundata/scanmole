"""Tests for the GUI's advisory probe bookkeeping (no GTK, no hardware)."""

from __future__ import annotations

from scanmole.negotiation import Support, advisory_faint_assessment
from scanmole.options import Capability
from scanmole_gui.probing import ProbeCoordinator, ProbeRequest, selection_blocked


def test_coordinator_serializes_probes() -> None:
    coordinator = ProbeCoordinator()
    first = ProbeRequest("epsonds:net:1")
    second = ProbeRequest("epsonds:net:1", (("--source", "ADF Duplex"),))

    token = coordinator.begin(first)
    queued = coordinator.begin(second)

    assert token is not None
    assert queued is None  # runs after the first completes

    current, follow_up = coordinator.complete(token, {"mode": object()})
    assert current is True
    assert follow_up == second


def test_stale_results_are_rejected() -> None:
    coordinator = ProbeCoordinator()
    stale_token = coordinator.begin(ProbeRequest("dev-a"))
    assert stale_token is not None
    # The user switches devices: the running probe becomes obsolete.
    coordinator.complete(stale_token, None)
    fresh_token = coordinator.begin(ProbeRequest("dev-b"))
    assert fresh_token is not None

    current, _ = coordinator.complete(stale_token, {"old": object()})

    assert current is False  # dev-a's late answer must be dropped
    current, _ = coordinator.complete(fresh_token, {"new": object()})
    assert current is True


def test_cache_keys_include_the_applied_settings() -> None:
    coordinator = ProbeCoordinator()
    bare = ProbeRequest("dev")
    sourced = ProbeRequest("dev", (("--source", "ADF Duplex"),))
    token = coordinator.begin(bare)
    assert token is not None
    coordinator.complete(token, {"bare": True})

    hit_bare, snapshot = coordinator.cached(bare)
    hit_sourced, _ = coordinator.cached(sourced)

    assert hit_bare is True and snapshot == {"bare": True}
    assert hit_sourced is False  # a different applied state


def test_forget_drops_only_the_named_device() -> None:
    coordinator = ProbeCoordinator()
    for device in ("dev-a", "dev-b"):
        token = coordinator.begin(ProbeRequest(device))
        assert token is not None
        coordinator.complete(token, {device: True})

    coordinator.forget("dev-a")

    assert coordinator.cached(ProbeRequest("dev-a"))[0] is False
    assert coordinator.cached(ProbeRequest("dev-b"))[0] is True


def test_faint_availability_follows_the_source_applied_probe() -> None:
    # Mirrors the app flow: the snapshot probed with the selected source
    # applied decides the faint choice via the advisory verdict. A device
    # that conclusively offers only plain 1-bit blocks the choice; one with
    # a gray mode (or a visible native-enhancement signature) keeps it
    # selectable, and the engine confirms the actual path at scan time.
    coordinator = ProbeCoordinator()
    lineart_only = ProbeRequest("dev-a", (("--source", "ADF"),))
    gray_capable = ProbeRequest("dev-b", (("--source", "ADF"),))
    for request, caps in (
        (lineart_only, {"mode": Capability(kind="enum", choices=["Lineart"])}),
        (gray_capable, {"mode": Capability(kind="enum", choices=["Lineart", "Gray"])}),
    ):
        token = coordinator.begin(request)
        assert token is not None
        coordinator.complete(token, caps)

    verdicts = {}
    for name, request in (("dev-a", lineart_only), ("dev-b", gray_capable)):
        hit, snapshot = coordinator.cached(request)
        assert hit is True
        assert isinstance(snapshot, dict)
        verdicts[name] = advisory_faint_assessment(snapshot)

    assert selection_blocked(verdicts["dev-a"].support) is True
    assert "ordinary B/W" in verdicts["dev-a"].consequence
    assert selection_blocked(verdicts["dev-b"].support) is False


def test_stale_candidate_probe_never_downgrades_a_newer_selection() -> None:
    # The user switches devices while a candidate probe is in flight: its
    # late lineart-only answer must not block the faint choice of the newly
    # selected, gray-capable device.
    coordinator = ProbeCoordinator()
    stale = coordinator.begin(ProbeRequest("dev-a", (("--source", "ADF"),)))
    assert stale is not None
    coordinator.complete(stale, None)  # the switch obsoletes the probe
    fresh = coordinator.begin(ProbeRequest("dev-b", (("--source", "ADF"),)))
    assert fresh is not None

    current, _ = coordinator.complete(
        stale, {"mode": Capability(kind="enum", choices=["Lineart"])}
    )

    assert current is False  # the caller must drop it before any blocking


def test_source_refinement_cannot_displace_a_queued_bare_probe() -> None:
    # The regression sequence: while device A's probe runs, selecting B
    # queues B's mandatory bare probe. A source-change callback then asks
    # for B with a source applied; that refinement must not replace the
    # queued bare request (B would be assessed with A's availability). It
    # is re-derived from the newest selection once B's bare snapshot lands.
    coordinator = ProbeCoordinator()
    a_token = coordinator.begin(ProbeRequest("dev-a"))
    assert a_token is not None
    assert coordinator.begin(ProbeRequest("dev-b")) is None  # queued
    assert coordinator.begin(ProbeRequest("dev-b", (("--source", "ADF"),))) is None

    current, follow_up = coordinator.complete(a_token, {"a": True})

    assert current is True
    assert follow_up == ProbeRequest("dev-b")  # the bare probe survived

    b_token = coordinator.begin(follow_up)
    assert b_token is not None
    coordinator.complete(b_token, {"b": True})
    assert coordinator.cached(ProbeRequest("dev-b"))[0] is True
    # With the bare snapshot cached, the refinement may run normally.
    assert coordinator.begin(ProbeRequest("dev-b", (("--source", "ADF"),)))


def test_rapid_switching_keeps_the_newest_bare_probe() -> None:
    # A -> B -> A while A's probe still runs: the newest bare request wins
    # the queue slot; the obsolete intermediate is dropped.
    coordinator = ProbeCoordinator()
    a_token = coordinator.begin(ProbeRequest("dev-a"))
    assert a_token is not None
    assert coordinator.begin(ProbeRequest("dev-b")) is None
    assert coordinator.begin(ProbeRequest("dev-a")) is None

    _, follow_up = coordinator.complete(a_token, {"a": True})

    assert follow_up == ProbeRequest("dev-a")


def test_stale_completion_never_disturbs_the_running_probe() -> None:
    # A slow worker's late completion must neither clear the newer running
    # probe nor steal its queued follow-up.
    coordinator = ProbeCoordinator()
    stale = coordinator.begin(ProbeRequest("dev-a"))
    assert stale is not None
    coordinator.complete(stale, None)  # the device switch obsoletes it
    fresh = coordinator.begin(ProbeRequest("dev-b"))
    assert fresh is not None
    assert coordinator.begin(ProbeRequest("dev-c")) is None  # queued

    current, follow_up = coordinator.complete(stale, {"old": True})

    assert current is False
    assert follow_up is None  # the queue belongs to the running probe
    current, follow_up = coordinator.complete(fresh, {"new": True})
    assert current is True
    assert follow_up == ProbeRequest("dev-c")
    assert coordinator.cached(ProbeRequest("dev-b")) == (True, {"new": True})


def test_probe_failure_is_cached_and_does_not_wedge_the_queue() -> None:
    # A failed probe (None snapshot) is a result too: it caches, the
    # queued follow-up still comes back, and nothing stays "running".
    coordinator = ProbeCoordinator()
    token = coordinator.begin(ProbeRequest("dev-a"))
    assert token is not None
    assert coordinator.begin(ProbeRequest("dev-b")) is None

    current, follow_up = coordinator.complete(token, None)

    assert current is True
    assert follow_up == ProbeRequest("dev-b")
    assert coordinator.cached(ProbeRequest("dev-a")) == (True, None)
    assert coordinator.begin(ProbeRequest("dev-b")) is not None  # queue is free


def test_selection_blocking_policy() -> None:
    # NATIVE, EMULATED and UNKNOWN stay selectable; lossy paths do not.
    assert selection_blocked(Support.NATIVE) is False
    assert selection_blocked(Support.EMULATED) is False
    assert selection_blocked(Support.UNKNOWN) is False
    assert selection_blocked(Support.DEGRADED) is True
    assert selection_blocked(Support.UNSUPPORTED) is True


# ------------------------------------------------- CapabilityFlow (staged)

from scanmole_gui.probing import CapabilityFlow  # noqa: E402


def _caps(*sources: str, modes: tuple[str, ...] = ("Lineart", "Gray", "Color")):  # type: ignore[no-untyped-def]
    return {
        "source": Capability(kind="enum", choices=list(sources)),
        "mode": Capability(kind="enum", choices=list(modes)),
    }


def test_flow_probes_bare_first_on_device_selection() -> None:
    flow = CapabilityFlow()

    update = flow.select_device("dev-a", False, "adf-duplex")

    assert update.start_probe is not None
    _token, request = update.start_probe
    assert request == ProbeRequest("dev-a")
    assert update.source_blocked is None  # nothing known yet


def test_flow_never_probes_while_scanning() -> None:
    flow = CapabilityFlow()

    assert flow.select_device("dev-a", True, "adf-duplex").start_probe is None
    assert flow.change_source("dev-a", True, "adf", True).start_probe is None


def test_flow_applies_bare_snapshot_and_requests_the_follow_up() -> None:
    flow = CapabilityFlow()
    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe

    update = flow.probe_completed(
        token, request, _caps("ADF Duplex", "ADF Front"), "dev-a", "adf-duplex"
    )

    assert update.source_blocked is not None
    assert "flatbed" in update.source_blocked  # no flatbed on this device
    assert "adf-duplex" not in update.source_blocked
    assert update.start_probe is not None  # the source-applied follow-up
    _token, follow = update.start_probe
    assert follow.settings == (("--source", "ADF Duplex"),)
    assert update.mode_blocked == {}  # gray offered: everything selectable


def test_flow_drops_stale_completions_for_other_devices() -> None:
    flow = CapabilityFlow()
    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe

    update = flow.probe_completed(
        token, request, _caps("ADF Duplex"), "dev-b", "adf-duplex"
    )

    assert update.source_blocked is None  # the user moved on; nothing applied


def test_flow_queues_while_another_probe_runs_and_recovers() -> None:
    flow = CapabilityFlow()
    first = flow.select_device("dev-a", False, "adf-duplex")
    assert first.start_probe is not None
    queued = flow.select_device("dev-b", False, "adf-duplex")
    assert queued.start_probe is None  # queued behind dev-a's probe

    token, request = first.start_probe
    update = flow.probe_completed(
        token, request, _caps("ADF Duplex"), "dev-b", "adf-duplex"
    )

    assert update.start_probe is not None  # dev-b's bare probe starts now
    assert update.start_probe[1] == ProbeRequest("dev-b")
    assert update.source_blocked is None  # dev-a's stale result not applied


def test_flow_requires_the_devices_own_bare_snapshot_for_refinement() -> None:
    # A source change while another device's snapshot is current must not
    # judge the new device with foreign availability; the new device's
    # bare probe runs first (queued behind the in-flight follow-up).
    flow = CapabilityFlow()
    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe
    done = flow.probe_completed(
        token, request, _caps("ADF Duplex"), "dev-a", "adf-duplex"
    )
    assert done.start_probe is not None  # dev-a's source-applied follow-up

    switched = flow.select_device("dev-b", False, "adf-duplex")
    assert switched.start_probe is None  # queued behind dev-a's follow-up
    update = flow.change_source("dev-b", False, "adf", True)
    assert update.start_probe is None  # no refinement from dev-a's snapshot

    follow_token, follow_request = done.start_probe
    finished = flow.probe_completed(
        follow_token, follow_request, _caps("ADF Duplex"), "dev-b", "adf"
    )
    assert finished.start_probe is not None
    assert finished.start_probe[1] == ProbeRequest("dev-b")  # bare comes first
    assert finished.source_blocked is None  # dev-a's stale result not applied


def test_flow_failed_probe_logs_once_and_keeps_everything_selectable() -> None:
    flow = CapabilityFlow()
    first = flow.select_device("dev-a", False, "adf-duplex")
    assert first.start_probe is not None
    token, request = first.start_probe

    update = flow.probe_completed(token, request, None, "dev-a", "adf-duplex")

    assert update.log_probe_failure is True
    assert update.source_blocked == {}  # UNKNOWN stays selectable
    assert update.mode_blocked == {}
    assert update.start_probe is None  # no follow-up without capabilities

    second = flow.select_device("dev-b", False, "adf-duplex")
    assert second.start_probe is not None
    token, request = second.start_probe
    again = flow.probe_completed(token, request, None, "dev-b", "adf-duplex")
    assert again.log_probe_failure is False  # logged once per window


def test_flow_mode_dependent_capabilities_block_the_faint_mode() -> None:
    flow = CapabilityFlow()
    started = flow.select_device("dev-a", False, "adf")
    assert started.start_probe is not None
    token, request = started.start_probe
    bare = flow.probe_completed(token, request, _caps("ADF Front"), "dev-a", "adf")
    assert bare.start_probe is not None
    token, follow = bare.start_probe

    update = flow.probe_completed(
        token, follow, _caps("ADF Front", modes=("Lineart",)), "dev-a", "adf"
    )

    assert update.mode_blocked is not None
    assert "lineart-auto" in update.mode_blocked  # conclusively 1-bit only
    assert "gray" in update.mode_blocked


def test_flow_adopts_the_sole_source_and_keeps_the_preference() -> None:
    flow = CapabilityFlow(preferred_source="adf-duplex")
    started = flow.select_device("ix100", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe

    update = flow.probe_completed(
        token, request, _caps("ADF Front"), "ix100", "adf-duplex"
    )

    assert update.select_source == "adf"
    assert update.adopted_sole_source == "adf"
    assert flow.preferred_source == "adf-duplex"  # untouched by adoption


def test_flow_restores_the_manual_preference_when_available() -> None:
    flow = CapabilityFlow(preferred_source="adf-duplex")
    started = flow.select_device("dev-a", False, "adf")
    assert started.start_probe is not None
    token, request = started.start_probe

    update = flow.probe_completed(
        token, request, _caps("ADF Duplex", "ADF Front"), "dev-a", "adf"
    )

    assert update.select_source == "adf-duplex"  # the preference comes back
    assert update.adopted_sole_source is None  # a restore, not an adoption


def test_flow_keeps_a_blocked_choice_when_a_real_choice_remains() -> None:
    flow = CapabilityFlow(preferred_source="adf-back")
    started = flow.select_device("dev-a", False, "adf-back")
    assert started.start_probe is not None
    token, request = started.start_probe

    update = flow.probe_completed(
        token, request, _caps("ADF Duplex", "ADF Front"), "dev-a", "adf-back"
    )

    assert update.select_source is None  # never changed silently
    assert update.source_blocked is not None
    assert "adf-back" in update.source_blocked


def test_flow_native_faint_evidence_keeps_the_choice_selectable() -> None:
    flow = CapabilityFlow()
    started = flow.select_device("dev-a", False, "adf")
    assert started.start_probe is not None
    token, request = started.start_probe
    caps = {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "mode": Capability(kind="enum", choices=["Lineart"]),
        "halftoning": Capability(
            kind="enum", choices=["None", "Text Enhanced Technology"]
        ),
    }

    update = flow.probe_completed(token, request, caps, "dev-a", "adf")

    assert update.mode_blocked is not None
    assert "lineart-auto" not in update.mode_blocked  # engine verifies later


def test_flow_manual_source_change_updates_the_preference() -> None:
    flow = CapabilityFlow(preferred_source="adf-duplex")

    flow.change_source("dev-a", False, "flatbed", manual=True)
    assert flow.preferred_source == "flatbed"

    flow.change_source("dev-a", False, "adf", manual=False)
    assert flow.preferred_source == "flatbed"  # programmatic selects never do


def _flow_with_pending_adf_refinement() -> tuple[CapabilityFlow, int, ProbeRequest]:
    # A device with a feeder and a flatbed: the bare snapshot applied,
    # the ADF Duplex refinement in flight.
    flow = CapabilityFlow(preferred_source="adf-duplex")
    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe
    done = flow.probe_completed(
        token, request, _caps("ADF Duplex", "Flatbed"), "dev-a", "adf-duplex"
    )
    assert done.start_probe is not None
    adf_token, adf_request = done.start_probe
    assert adf_request.settings == (("--source", "ADF Duplex"),)
    return flow, adf_token, adf_request


def test_flow_never_renders_a_completed_probe_of_an_obsolete_source() -> None:
    # The user switches to the flatbed while the ADF refinement is in
    # flight. The feeder's full-mode result must not govern the flatbed:
    # it would unblock B/W on a color-only flatbed until the flatbed's
    # own probe lands.
    flow, adf_token, adf_request = _flow_with_pending_adf_refinement()
    switched = flow.change_source("dev-a", False, "flatbed", True)
    assert switched.start_probe is None  # queued behind the ADF refinement

    stale = flow.probe_completed(
        adf_token, adf_request, _caps("ADF Duplex", "Flatbed"), "dev-a", "flatbed"
    )

    assert stale.mode_blocked is None  # an obsolete source renders nothing
    assert stale.start_probe is not None  # the flatbed refinement starts now
    assert stale.start_probe[1].settings == (("--source", "Flatbed"),)

    flat_token, flat_request = stale.start_probe
    current = flow.probe_completed(
        flat_token,
        flat_request,
        _caps("ADF Duplex", "Flatbed", modes=("Color",)),
        "dev-a",
        "flatbed",
    )
    assert current.mode_blocked is not None
    # The flatbed's own truth: gray degrades on a color-only flatbed
    # (lineart stays selectable through software binarization).
    assert "gray" in current.mode_blocked

    # The obsolete feeder result stays cached: returning to the feeder
    # renders it immediately without another probe.
    back = flow.change_source("dev-a", False, "adf-duplex", True)
    assert back.start_probe is None
    assert back.mode_blocked == {}


def test_flow_obsolete_source_result_cannot_block_a_valid_choice() -> None:
    # The reverse race: a color-only feeder result finishing after the
    # switch must not block modes the flatbed genuinely offers.
    flow, adf_token, adf_request = _flow_with_pending_adf_refinement()
    flow.change_source("dev-a", False, "flatbed", True)

    stale = flow.probe_completed(
        adf_token,
        adf_request,
        _caps("ADF Duplex", "Flatbed", modes=("Color",)),
        "dev-a",
        "flatbed",
    )
    assert stale.mode_blocked is None  # the feeder's limits stay its own

    assert stale.start_probe is not None
    flat_token, flat_request = stale.start_probe
    current = flow.probe_completed(
        flat_token, flat_request, _caps("ADF Duplex", "Flatbed"), "dev-a", "flatbed"
    )
    assert current.mode_blocked == {}  # everything the flatbed offers
