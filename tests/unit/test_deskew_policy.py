"""Tests for deskew ownership, decided from a capability listing alone.

The policy is a pure function over capabilities, so the states it has to
tell apart are enumerable and every one of them is here: absent, one the
command can set, one read-only and reporting yes, one read-only and
reporting no, and one read-only that will not say. The last is the
interesting one, because it is the only state where nothing can be
established and every method has to refuse.
"""

from __future__ import annotations

import pytest

from scanmole.config import DeskewMethod
from scanmole.deskew_policy import (
    MECHANISMS,
    QUALIFIED_FOR_AUTO,
    DeskewOwner,
    DeskewPlan,
    plan_deskew,
)
from scanmole.devices import backend_name
from scanmole.errors import DeviceError
from scanmole.options import Capability, parse_capabilities

WRITABLE = "    --{name}[=(yes|no)] [no]\n"
STUCK_ON = "    --{name}[=(yes|no)] [yes] [read-only]\n"
STUCK_OFF = "    --{name}[=(yes|no)] [no] [read-only]\n"
UNREADABLE = "    --{name}[=(yes|no)] [read-only]\n"
INACTIVE = "    --{name}[=(yes|no)] [no] [inactive]\n"


def _caps(*lines: str) -> dict[str, Capability]:
    return parse_capabilities("    --mode Lineart [Lineart]\n" + "".join(lines))


def _plan(
    *lines: str,
    requested: bool = True,
    method: DeskewMethod = "auto",
    backend: str = "fujitsu",
) -> DeskewPlan:
    return plan_deskew(
        _caps(*lines), requested=requested, method=method, backend=backend
    )


# ------------------------------------------------------------- the default


def test_nothing_is_qualified_for_automatic_backend_ownership() -> None:
    # Adding a name to this set is the whole change needed to let auto
    # prefer a backend mechanism, so an accidental entry has to be as
    # visible as a behaviour change.
    assert QUALIFIED_FOR_AUTO == frozenset()


@pytest.mark.parametrize("name", MECHANISMS)
def test_automatic_keeps_the_request_and_shuts_the_mechanism(name: str) -> None:
    plan = _plan(WRITABLE.format(name=name))

    assert plan.owner is DeskewOwner.HOST
    assert plan.applied is False
    assert plan.options == ((name, "no"),)
    assert plan.notice is None


def test_automatic_without_any_mechanism_is_still_the_host() -> None:
    plan = _plan()

    assert plan.owner is DeskewOwner.HOST
    assert plan.options == ()


@pytest.mark.parametrize("lines", [STUCK_OFF, INACTIVE])
def test_a_mechanism_proven_harmless_leaves_the_host_in_charge(
    lines: str,
) -> None:
    # Read-only reporting no, and inactive, both say the same thing: the
    # command cannot settle this and the device will not do it either.
    plan = _plan(lines.format(name="swdeskew"))

    assert plan.owner is DeskewOwner.HOST
    assert plan.options == ()


# ------------------------------------------------------------ forced host


@pytest.mark.parametrize("name", MECHANISMS)
def test_forcing_scanmole_shuts_every_mechanism(name: str) -> None:
    plan = _plan(WRITABLE.format(name=name), method="scanmole")

    assert plan.owner is DeskewOwner.HOST
    assert plan.options == ((name, "no"),)


@pytest.mark.parametrize("name", MECHANISMS)
def test_forcing_scanmole_refuses_a_device_that_deskews_anyway(name: str) -> None:
    with pytest.raises(DeviceError, match="twice"):
        _plan(STUCK_ON.format(name=name), method="scanmole")


# --------------------------------------------------------- forced backend


@pytest.mark.parametrize("name", MECHANISMS)
def test_forcing_the_scanner_enables_the_mechanism(name: str) -> None:
    plan = _plan(WRITABLE.format(name=name), method="scanner")

    assert plan.owner is DeskewOwner.BACKEND
    assert plan.applied is True
    assert plan.options == ((name, "yes"),)


@pytest.mark.parametrize("name", MECHANISMS)
def test_forcing_the_scanner_accepts_one_that_is_already_on(name: str) -> None:
    # Read-only and already yes is backend ownership, just not one the
    # command established. Asking for the scanner and getting it is not
    # a reason to refuse.
    plan = _plan(STUCK_ON.format(name=name), method="scanner")

    assert plan.owner is DeskewOwner.BACKEND
    assert plan.options == ()


@pytest.mark.parametrize("lines", ["", STUCK_OFF, INACTIVE])
def test_forcing_the_scanner_refuses_without_a_usable_mechanism(lines: str) -> None:
    # Anything short of proof is a refusal here, because the fallback
    # would be the owner the run explicitly did not ask for.
    with pytest.raises(DeviceError, match="no deskew option"):
        _plan(lines.format(name="swdeskew") if lines else "", method="scanner")


# ------------------------------------------------------------ exclusivity


def test_only_one_mechanism_is_ever_turned_on() -> None:
    plan = _plan(
        WRITABLE.format(name="swdeskew"),
        WRITABLE.format(name="adf-skew"),
        method="scanner",
    )

    assert plan.options == (("swdeskew", "yes"), ("adf-skew", "no"))


def test_a_stuck_mechanism_keeps_the_others_off() -> None:
    # The stuck one owns the request; enabling the settable one as well
    # would rotate the page twice inside the backend.
    plan = _plan(
        STUCK_ON.format(name="swdeskew"),
        WRITABLE.format(name="adf-skew"),
        method="scanner",
    )

    assert plan.owner is DeskewOwner.BACKEND
    assert plan.options == (("adf-skew", "no"),)


def test_an_unavoidable_mechanism_takes_the_request_under_automatic() -> None:
    # The one case where auto does not mean ScanMole. The host must be
    # kept out (applied is what stops it) and the user told why.
    plan = _plan(STUCK_ON.format(name="swdeskew"))

    assert plan.owner is DeskewOwner.BACKEND
    assert plan.applied is True
    assert plan.notice is not None
    assert "read-only" in plan.notice


# ------------------------------------------------------------- no request


@pytest.mark.parametrize("method", ["auto", "scanmole", "scanner"])
def test_no_request_shuts_every_mechanism_whatever_the_method(
    method: DeskewMethod,
) -> None:
    plan = _plan(
        WRITABLE.format(name="swdeskew"),
        WRITABLE.format(name="adf-skew"),
        requested=False,
        method=method,
    )

    assert plan.owner is DeskewOwner.NONE
    assert plan.applied is False
    assert plan.options == (("swdeskew", "no"), ("adf-skew", "no"))


@pytest.mark.parametrize("name", MECHANISMS)
def test_no_request_refuses_a_device_that_deskews_anyway(name: str) -> None:
    # The pages would come back straightened while the run reported them
    # untouched, about paper the user may no longer have.
    with pytest.raises(DeviceError, match="--no-deskew cannot be honored"):
        _plan(STUCK_ON.format(name=name), requested=False)


def _qualify(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> None:
    monkeypatch.setattr("scanmole.deskew_policy.QUALIFIED_FOR_AUTO", frozenset(pairs))


def test_the_qualification_seam_moves_ownership_without_a_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What passing the gate would buy: one pair in the set, and auto
    # starts preferring that mechanism. Nothing else in the policy moves.
    _qualify(monkeypatch, ("fujitsu", "swdeskew"))

    qualified = _plan(WRITABLE.format(name="swdeskew"))
    other = _plan(WRITABLE.format(name="adf-skew"))

    assert qualified.owner is DeskewOwner.BACKEND
    assert qualified.options == (("swdeskew", "yes"),)
    assert other.owner is DeskewOwner.HOST  # unqualified, still ScanMole's


def test_qualifying_one_backend_says_nothing_about_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole reason the key is a pair. Two backends may spell an
    # option the same way and mean entirely different code, so evidence
    # gathered about one must not travel to the other.
    _qualify(monkeypatch, ("fujitsu", "swdeskew"))
    listing = WRITABLE.format(name="swdeskew")

    measured = _plan(listing, backend="fujitsu")
    lookalike = _plan(listing, backend="epsonds")

    assert measured.owner is DeskewOwner.BACKEND
    assert lookalike.owner is DeskewOwner.HOST


def test_an_unknown_backend_qualifies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No name, no evidence: the safe reading of "we could not tell which
    # driver this is" is that none of it has been measured.
    _qualify(monkeypatch, ("fujitsu", "swdeskew"))

    assert _plan(WRITABLE.format(name="swdeskew"), backend="").owner is DeskewOwner.HOST


def test_qualification_never_overrides_an_unavoidable_mechanism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A qualified mechanism is one measured to straighten well, not a
    # licence to add a second correction to a page the device is already
    # straightening. The unavoidable one owns and the qualified one is
    # switched off with everything else.
    _qualify(monkeypatch, ("fujitsu", "adf-skew"))

    plan = _plan(STUCK_ON.format(name="swdeskew"), WRITABLE.format(name="adf-skew"))

    assert plan.owner is DeskewOwner.BACKEND
    assert plan.options == (("adf-skew", "no"),)


def test_the_backend_name_carries_no_more_than_the_backend() -> None:
    # The rest of a SANE identifier is a serial number, a USB path or a
    # hostname. None of it may reach a policy decision, so none of it
    # survives the trip.
    assert backend_name("fujitsu:ScanSnap iX100:1209870") == "fujitsu"
    assert backend_name("airscan:e0:Brother ADS-4550W (USB)") == "airscan"
    assert backend_name("net:192.168.1.5:epson2:dev0") == "net"
    assert backend_name("test") == "test"


# ------------------------------------------------- unknowable listings


@pytest.mark.parametrize(
    "lines", [UNREADABLE, "    --{name}[=(yes|no)] [?] [read-only]\n"]
)
@pytest.mark.parametrize("method", ["auto", "scanmole", "scanner"])
@pytest.mark.parametrize("requested", [True, False])
def test_an_unreadable_mechanism_refuses_under_every_method(
    lines: str, method: DeskewMethod, requested: bool
) -> None:
    # Active, so the option is real, and unreadable, so it may be
    # straightening every page or none. Neither host nor backend
    # exclusivity can be established, and that holds however the request
    # was phrased, --no-deskew included.
    with pytest.raises(DeviceError, match="reports no value"):
        _plan(lines.format(name="swdeskew"), requested=requested, method=method)


@pytest.mark.parametrize("method", ["auto", "scanmole", "scanner"])
@pytest.mark.parametrize("requested", [True, False])
def test_two_unavoidable_mechanisms_refuse_under_every_method(
    method: DeskewMethod, requested: bool
) -> None:
    # Two corrections nobody can stop. Exactly one per page is the whole
    # invariant, so there is nothing to choose between and nothing to
    # report honestly.
    with pytest.raises(DeviceError, match="both read-only"):
        _plan(
            STUCK_ON.format(name="swdeskew"),
            STUCK_ON.format(name="adf-skew"),
            requested=requested,
            method=method,
        )


@pytest.mark.parametrize("method", ["auto", "scanmole", "scanner"])
@pytest.mark.parametrize("requested", [True, False])
def test_an_unavoidable_mechanism_beside_an_unreadable_one_refuses(
    method: DeskewMethod, requested: bool
) -> None:
    # One is definitely correcting and the other may be. The count of
    # corrections is unknown, so exactly one cannot be established.
    with pytest.raises(DeviceError):
        _plan(
            STUCK_ON.format(name="swdeskew"),
            UNREADABLE.format(name="adf-skew"),
            requested=requested,
            method=method,
        )
