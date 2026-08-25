"""Who straightens a scanned page, settled before any paper moves.

Two mechanisms can do the job and running both costs more sharpness than
the skew they remove, so exactly one owns a run: ScanMole's own host path
(:mod:`scanmole.deskew`) or a backend option the scanner exposes. This
module decides which, from the capability listing alone, and returns the
options that establish it. Deciding it here rather than while pages land
is the point: an owner nobody can honour has to refuse while the stack is
still in the feeder.

The default is ScanMole's own path. That is not a claim that backends
straighten badly, it is a claim about what is known: a backend mechanism
is a black box whose result nobody here has measured, while the host path
behaves the same on every device and has corpus evidence behind it. A
mechanism earns automatic ownership by passing the qualification gate in
the scanner evidence runbook, which measures residual angle against
printed targets rather than against the tool ScanMole would otherwise
use. Until one passes, :data:`QUALIFIED_FOR_AUTO` stays empty.

Qualification is keyed on a backend and one of its options, never on a
device model. A model list would have to grow with every product a
vendor ships against the same driver and would say nothing about the
driver that does the work; an option name alone is worse still, because
two backends can spell the same word and mean different code. Only the
SANE prefix travels here (:func:`scanmole.devices.backend_name`), so the
serial number the rest of an identifier carries never reaches a
decision, a log line or a document.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from scanmole.config import DeskewMethod
from scanmole.errors import DeviceError
from scanmole.options import Capability, readable_capability

MECHANISMS = ("swdeskew", "adf-skew")
"""Backend deskew options ScanMole knows how to drive, in priority order.

``swdeskew`` is the ``fujitsu`` backend's software deskew, ``adf-skew``
the ``epsonds`` backend's hardware skew correction. Both take yes/no and
mean the same thing to a caller, so the order only decides which one
owns a device that somehow offered both.
"""

QUALIFIED_FOR_AUTO: frozenset[tuple[str, str]] = frozenset()
"""``(backend, option)`` pairs allowed to take the request under ``auto``.

Empty until one passes the gate. Adding a pair here is the entire change
needed to let ``auto`` prefer a backend mechanism, which is why the seam
is data and not a branch. The pair is the identity that matters: a
mechanism is qualified by the driver that implements it together with
the option that drives it, so measuring ``fujitsu``'s ``swdeskew`` says
nothing about an option of the same name in another backend.

``("fujitsu", "swdeskew")`` was measured on a real ScanSnap iX500
(2026-08-25, see ARCHITECTURE.md's deskew evidence): at a consistent
~3.7 degree hand-fed skew it removed about 15% of the rotation and left
a residual roughly sixteen times the gate's 0.20 degree cap. It does
correct something, just not enough, and stays out of this set.
"""

_TRUE = frozenset({"yes", "true", "on", "1"})
_FALSE = frozenset({"no", "false", "off", "0"})


class DeskewOwner(enum.Enum):
    """The single mechanism that straightens the pages of a run."""

    HOST = "host"
    """ScanMole rotates each raw frame itself."""
    BACKEND = "backend"
    """A scanner option straightens the page before ScanMole sees it."""
    NONE = "none"
    """Nothing straightens anything; the pages keep their skew."""


@dataclass(frozen=True)
class DeskewPlan:
    """Settled ownership, plus the options that establish it.

    ``options`` are ``(name, value)`` pairs the scan command must emit,
    including the ``no`` values that keep the mechanisms nobody chose out
    of the way: leaving one unmentioned would let a device default
    straighten a page the run already accounted for.
    """

    owner: DeskewOwner
    options: tuple[tuple[str, str], ...] = ()
    notice: str | None = None
    """A warning the caller should log once, or ``None``.

    Used where the outcome is honest but not what was asked for, which
    is worth a sentence and is not worth refusing over.
    """

    @property
    def applied(self) -> bool:
        """Whether the backend takes the request.

        The one boolean downstream reads, so a change of policy here
        never reaches the pipeline as a new concept.
        """
        return self.owner is DeskewOwner.BACKEND


class _State(enum.Enum):
    """What a capability listing proves about one deskew mechanism.

    Deliberately five states rather than a boolean and a ``None``: the
    difference between "read-only and reports no" and "read-only and
    will not say" decides whether a scan may run at all, and collapsing
    both into "nothing to worry about" is exactly the mistake that let a
    device straighten pages nobody accounted for.
    """

    ABSENT = "absent"
    """Not listed, or listed inactive. Nothing there to drive or fear."""
    SETTABLE = "settable"
    """The scan command decides what this one does."""
    FORCED_ON = "forced-on"
    """Read-only and reports yes: it will straighten, whatever we ask."""
    FORCED_OFF = "forced-off"
    """Read-only and reports no: it will not, and cannot be made to."""
    OPAQUE = "opaque"
    """Read-only with no value anyone can read.

    Active, so the option is real, yet nothing about it is knowable: it
    may be straightening every page or none. That is not the same as off
    and must never be treated as it, because both possible answers
    matter and neither can be checked.
    """


def _state(caps: dict[str, Capability], name: str) -> _State:
    """Classify one mechanism from the listing."""
    capability = readable_capability(caps, name)
    if capability is None:
        return _State.ABSENT
    if capability.settable:
        return _State.SETTABLE
    current = (capability.current or "").strip().lower()
    if current in _TRUE:
        return _State.FORCED_ON
    if current in _FALSE:
        return _State.FORCED_OFF
    return _State.OPAQUE


def _by_state(caps: dict[str, Capability]) -> dict[_State, tuple[str, ...]]:
    """Every mechanism grouped by what the listing proves, in priority order."""
    grouped: dict[_State, list[str]] = {state: [] for state in _State}
    for name in MECHANISMS:
        grouped[_state(caps, name)].append(name)
    return {state: tuple(names) for state, names in grouped.items()}


def _all_off(settable: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Turn every settable mechanism off."""
    return tuple((name, "no") for name in settable)


def _only(owner: str, settable: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Turn ``owner`` on and every other settable mechanism off.

    One mechanism per run, even where a device offers two. Two backend
    corrections over one page is the same mistake as a backend and a host
    correction, and no listing says whether the second one would be a
    no-op or a second rotation.
    """
    return tuple((name, "yes" if name == owner else "no") for name in settable)


def _forced_description(name: str) -> str:
    """How to say "the device does this and will not stop" in one clause."""
    return (
        f"the scanner straightens pages itself and offers no way to turn "
        f"it off (--{name} is read-only and reports yes)"
    )


def plan_deskew(
    caps: dict[str, Capability],
    *,
    requested: bool,
    method: DeskewMethod,
    backend: str = "",
) -> DeskewPlan:
    """Settle who straightens this run's pages.

    Args:
        caps: The capability listing the scan will run against.
        requested: Whether deskew was asked for at all (``--deskew``).
        method: The requested owner. ``auto`` picks, the other two
            demand and refuse rather than quietly picking the other one.
        backend: The SANE backend name, and only that (see
            :func:`scanmole.devices.backend_name`). Decides qualification
            together with the option name; an empty value simply
            qualifies nothing, which is the safe reading of "unknown".

    Returns:
        The settled plan, whose ``options`` the command must emit.

    Raises:
        DeviceError: If the requested ownership cannot be established.
            Raised while the paper is still in the feeder, because the
            alternative is a stack that comes out straightened twice, or
            reported as untouched while the scanner touched it.
    """
    grouped = _by_state(caps)
    settable = grouped[_State.SETTABLE]
    forced_on = grouped[_State.FORCED_ON]
    opaque = grouped[_State.OPAQUE]

    # Two refusals that hold for every method, including --no-deskew,
    # because neither depends on what was asked for: they are about the
    # device being unable to tell us what it does.
    if opaque:
        raise DeviceError(
            f"--{opaque[0]} is read-only and reports no value, so ScanMole "
            "cannot tell whether the scanner is already straightening "
            "pages; refusing to scan rather than guessing, since guessing "
            "wrong means either a page rotated twice or a page reported as "
            "untouched that was not"
        )
    if len(forced_on) > 1:
        raise DeviceError(
            f"--{forced_on[0]} and --{forced_on[1]} are both read-only and "
            "report yes, so the scanner applies two corrections and neither "
            "can be turned off; refusing to scan because exactly one "
            "correction per page cannot be established"
        )

    if forced_on:
        # Exactly one, and unavoidable. It owns the request whatever the
        # method prefers, so this is settled before qualification is even
        # consulted: nothing may be enabled beside a correction that is
        # already running.
        owner = forced_on[0]
        if not requested:
            raise DeviceError(
                f"{_forced_description(owner)}, so --no-deskew cannot be "
                "honored; refusing to scan rather than reporting the pages "
                "as unmodified"
            )
        if method == "scanmole":
            raise DeviceError(
                f"--deskew-method scanmole needs exclusive ownership, but "
                f"{_forced_description(owner)}; refusing to scan rather than "
                "straightening every page twice -- use --deskew-method "
                "scanner or auto to leave the job with the device"
            )
        notice = None
        if method == "auto":
            notice = (
                f"{_forced_description(owner)}, so the scanner keeps the "
                "deskew request and ScanMole does not straighten the pages "
                "again; pass --deskew-method scanmole to refuse such a "
                "device instead"
            )
        return DeskewPlan(DeskewOwner.BACKEND, _all_off(settable), notice=notice)

    if not requested:
        return DeskewPlan(DeskewOwner.NONE, _all_off(settable))

    if method == "scanner":
        if settable:
            return DeskewPlan(DeskewOwner.BACKEND, _only(settable[0], settable))
        raise DeviceError(
            "--deskew-method scanner needs the scanner to straighten pages, "
            "but this device exposes no deskew option ScanMole can drive; "
            "refusing to scan -- use --deskew-method scanmole or auto to let "
            "ScanMole straighten them instead"
        )

    if method == "scanmole":
        return DeskewPlan(DeskewOwner.HOST, _all_off(settable))

    qualified = next(
        (name for name in settable if (backend, name) in QUALIFIED_FOR_AUTO), None
    )
    if qualified is not None:
        return DeskewPlan(DeskewOwner.BACKEND, _only(qualified, settable))
    return DeskewPlan(DeskewOwner.HOST, _all_off(settable))
