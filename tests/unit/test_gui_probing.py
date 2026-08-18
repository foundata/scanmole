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


def test_selection_blocking_policy() -> None:
    # NATIVE, EMULATED and UNKNOWN stay selectable; lossy paths do not.
    assert selection_blocked(Support.NATIVE) is False
    assert selection_blocked(Support.EMULATED) is False
    assert selection_blocked(Support.UNKNOWN) is False
    assert selection_blocked(Support.DEGRADED) is True
    assert selection_blocked(Support.UNSUPPORTED) is True
