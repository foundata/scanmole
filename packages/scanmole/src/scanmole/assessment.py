"""The support model every capability verdict is expressed in.

One vocabulary shared by the general negotiation, the faint-originals
recognition, the engine and both frontends: how well a request is covered
(:class:`Support`), what one negotiated setting came to (:class:`Assessment`)
and what a whole scan request came to (:class:`Plan`).

Nothing here reads a device. The two helpers around emission encode the one
distinction the rest of the layer keeps running into: a read-only SANE option
may not be written, yet the device still sits on a value, and that value is
what a scan will really run in.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, replace

from scanmole.options import Capability


class Support(enum.Enum):
    """How well a requested setting is covered by the negotiated plan."""

    NATIVE = "native"
    """The scanner directly provides the requested semantics."""
    EMULATED = "emulated"
    """ScanMole software preserves the requested final semantics."""
    DEGRADED = "degraded"
    """Execution is possible but materially changes the request."""
    UNSUPPORTED = "unsupported"
    """Authoritative active capabilities prove there is no path."""
    UNKNOWN = "unknown"
    """Missing, inactive, failed or unparseable capability evidence."""


@dataclass(frozen=True)
class Assessment:
    """One negotiated setting.

    Attributes:
        requested: The ScanMole-level request (``adf-duplex``, ``lineart``,
            ``300``, ...).
        support: The support state (see :class:`Support`).
        reason: A stable, machine-usable reason code.
        consequence: Human-readable consequence for non-NATIVE outcomes.
        backend_value: The backend value the command will carry, or ``None``
            when the option is not passed at all.
        actual: The backend value the device will really be on, whether or
            not the command emits it. Equal to ``backend_value`` for a
            writable option; for a read-only one the command emits nothing
            yet the device still sits on a known value, and that is this.
            ``None`` means nothing was established, which is what an
            UNKNOWN verdict always yields: the request echoed back is not
            evidence of anything. Only options whose backend value is a
            distinct string (source and mode) carry one; the resolution's
            established value is its ``effective``.
        effective: The ScanMole-level semantics that will actually result.
    """

    requested: str
    support: Support
    reason: str
    consequence: str = ""
    backend_value: str | None = None
    actual: str | None = None
    effective: str = ""

    @property
    def conclusive(self) -> bool:
        """Whether real capability evidence backs this verdict.

        UNKNOWN means the listing proved nothing, so ``effective`` is only
        the request echoed back. Any decision that must not be taken on an
        unproven assumption asks this first.
        """
        return self.support is not Support.UNKNOWN


@dataclass(frozen=True)
class Plan:
    """A negotiated acquisition plan for one scan request.

    ``extra_options`` carries additional backend options the plan needs
    beyond source/mode/depth/resolution (a native faint-text enhancement's
    ordered settings), explicitly and in emission order; the mode's backend
    value is never overloaded with such arguments.
    """

    source: Assessment
    mode: Assessment
    depth: Assessment
    resolution: Assessment
    extra_options: tuple[tuple[str, str], ...] = ()


Settings = tuple[tuple[str, str], ...]

Prober = Callable[[Settings], "dict[str, Capability] | None"]
"""A capability probe with ordered settings applied; failure returns None."""


def state_choices(capability: Capability | None) -> list[str]:
    """The values a capability offers, or its current value when read-only.

    A ``[read-only]`` option lists no settable alternatives; what it does
    report is the value the device is using, and that is the only outcome
    a scan can have.
    """
    if capability is None:
        return []
    if capability.settable:
        return capability.choices
    current = (capability.current or "").strip()
    return [current] if current else []


def as_written(assessment: Assessment) -> Assessment:
    """Record an emitted value as the state the scan will run in."""
    return replace(assessment, actual=assessment.backend_value)


def as_read_only(assessment: Assessment) -> Assessment:
    """Keep an assessment's verdict and state while forbidding emission.

    A read-only option cannot be written, so nothing may be produced for
    it, but the value the match landed on is the one the device is on. It
    survives as ``actual`` only where the verdict rests on evidence: an
    UNKNOWN match would carry the request back rather than a fact.
    """
    matched = assessment.backend_value if assessment.conclusive else None
    return replace(assessment, backend_value=None, actual=matched)
