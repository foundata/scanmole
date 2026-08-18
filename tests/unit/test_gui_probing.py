"""Tests for the GUI's advisory probe bookkeeping (no GTK, no hardware)."""

from __future__ import annotations

from scanmole.negotiation import Support
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


def test_selection_blocking_policy() -> None:
    # NATIVE, EMULATED and UNKNOWN stay selectable; lossy paths do not.
    assert selection_blocked(Support.NATIVE) is False
    assert selection_blocked(Support.EMULATED) is False
    assert selection_blocked(Support.UNKNOWN) is False
    assert selection_blocked(Support.DEGRADED) is True
    assert selection_blocked(Support.UNSUPPORTED) is True
