"""Capability negotiation: what a device supports, and how well.

One shared support model tells the engine, the CLI and the GUI whether a
ScanMole setting is natively provided, equivalently emulated in software,
degraded, impossible, or simply unknown. Pure assessment over capability
snapshots; probing I/O has a thin helper, everything else takes data in and
returns a plan with structured notices out.

This models ScanMole's workflows (sources, modes, acquisition depth,
resolution), not arbitrary SANE options, and it is deliberately not a
complete SANE frontend. Matching stays evidence-based and fixture-pinned;
there are no device identity lists. Real backends are imperfect (the
DS-730N via epson2 reports an inactive Flatbed source on a sheet-fed
device), which is why missing or inactive evidence is UNKNOWN and stays
usable best-effort, never UNSUPPORTED.
"""

from __future__ import annotations

import enum
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from scanmole.config import LineartThreshold
from scanmole.errors import DeviceError
from scanmole.options import (
    _MODE_FALLBACKS,
    _MODE_PREDICATES,
    _SOURCE_FALLBACKS,
    _SOURCE_PREDICATES,
    Capability,
    _pick,
    active_capability,
    probe_capabilities,
    snap_resolution,
)

LOGGER = logging.getLogger(__name__)

ADVISORY_PROBE_TIMEOUT_SECONDS = 15.0
"""Timeout for advisory (GUI) capability probes.

Scan-time negotiation keeps the longer probe timeout and treats failure as
an error; an advisory probe turns timeout or failure into UNKNOWN instead,
so a slow or wedged backend cannot freeze a frontend.
"""


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
        effective: The ScanMole-level semantics that will actually result.
    """

    requested: str
    support: Support
    reason: str
    consequence: str = ""
    backend_value: str | None = None
    effective: str = ""


@dataclass(frozen=True)
class Plan:
    """A negotiated acquisition plan for one scan request."""

    source: Assessment
    mode: Assessment
    depth: Assessment
    resolution: Assessment


# Exact-source semantics per request. Stricter than the mapper's fallback
# predicates on purpose: an ADF Duplex choice must not count as an exact ADF
# simplex match just because a fuzzy feeder predicate accepts it.
_EXACT_SOURCE = {
    "flatbed": _SOURCE_PREDICATES["flatbed"],
    "adf-duplex": _SOURCE_PREDICATES["adf-duplex"],
    "adf-back": _SOURCE_PREDICATES["adf-back"],
    "adf": _SOURCE_PREDICATES["adf"][:2],  # strict simplex tiers only
}

_SOURCE_CONSEQUENCE = {
    ("adf-duplex", "adf"): "backs will not be scanned",
    ("adf-duplex", "flatbed"): (
        "one sheet per scan on the flatbed; backs will not be scanned"
    ),
    ("adf", "flatbed"): "one sheet per scan on the flatbed",
    ("adf-back", "adf"): "front sides will be scanned instead of backs",
    ("adf-back", "flatbed"): "one sheet per scan on the flatbed, front side",
}


def assess_source(caps: dict[str, Capability] | None, want: str) -> Assessment:
    """Negotiate the paper source for a request."""
    if caps is None:
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="probe-failed",
            consequence="capabilities could not be read; trying as requested",
            effective=want,
        )
    capability = active_capability(caps, "source")
    if capability is None or not capability.choices:
        inactive = caps.get("source") is not None
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="source-option-inactive" if inactive else "no-source-option",
            consequence="the device does not advertise usable sources; "
            "trying as requested",
            effective=want,
        )
    choices = capability.choices
    exact = _pick(choices, _EXACT_SOURCE[want])
    if exact is not None:
        return Assessment(
            requested=want,
            support=Support.NATIVE,
            reason="native-source",
            backend_value=exact,
            effective=want,
        )
    # A duplex feeder can serve a simplex request, but backs will also be
    # scanned: possible, materially different.
    if want == "adf":
        duplex = _pick(choices, _EXACT_SOURCE["adf-duplex"])
        if duplex is not None:
            return Assessment(
                requested=want,
                support=Support.DEGRADED,
                reason="only-duplex-feeder",
                consequence="back sides will also be scanned",
                backend_value=duplex,
                effective="adf-duplex",
            )
    for fallback in _SOURCE_FALLBACKS[want]:
        got = _pick(choices, _EXACT_SOURCE[fallback])
        if got is not None:
            return Assessment(
                requested=want,
                support=Support.DEGRADED,
                reason=f"no-{want}-source",
                consequence=_SOURCE_CONSEQUENCE.get(
                    (want, fallback), f"'{fallback}' is used instead"
                ),
                backend_value=got,
                effective=fallback,
            )
    return Assessment(
        requested=want,
        support=Support.UNSUPPORTED,
        reason="no-matching-source",
        consequence=(
            f"device has no source matching '{want}'; available: {', '.join(choices)}"
        ),
    )


def assess_mode(
    caps: dict[str, Capability] | None,
    want: str,
    lineart_threshold: LineartThreshold = 0.5,
) -> Assessment:
    """Negotiate the color mode for a request.

    ``want`` is ``lineart``, ``gray``, ``color`` or ``lineart-auto`` (the
    faint-originals variant of lineart, selected in the engine by
    ``--lineart-threshold auto``).
    """
    base = "lineart" if want == "lineart-auto" else want
    if caps is None:
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="probe-failed",
            consequence="capabilities could not be read; trying as requested",
            effective=want,
        )
    capability = active_capability(caps, "mode")
    if capability is None or not capability.choices:
        inactive = caps.get("mode") is not None
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="mode-option-inactive" if inactive else "no-mode-option",
            consequence="the device does not advertise usable modes; "
            "trying as requested",
            effective=want,
        )
    choices = capability.choices
    native = _pick(choices, _MODE_PREDICATES[base])
    if native is not None:
        if want == "lineart-auto":
            # The current acquisition prefers the device's own 1-bit mode,
            # and the adaptive threshold cannot operate on a P4 frame.
            return Assessment(
                requested=want,
                support=Support.DEGRADED,
                reason="native-1bit-defeats-adaptive",
                consequence=(
                    "the device scans 1-bit itself; the faint-originals "
                    "threshold cannot be applied"
                ),
                backend_value=native,
                effective="lineart",
            )
        return Assessment(
            requested=want,
            support=Support.NATIVE,
            reason="native-mode",
            backend_value=native,
            effective=want,
        )
    for fallback in _MODE_FALLBACKS[base]:
        got = _pick(choices, _MODE_PREDICATES[fallback])
        if got is None:
            continue
        if base == "lineart" and fallback in ("gray", "color"):
            if lineart_threshold != 0:
                return Assessment(
                    requested=want,
                    support=Support.EMULATED,
                    reason="software-1bit",
                    consequence=(
                        f"the device scans '{got}'; ScanMole converts to "
                        "1-bit in software"
                    ),
                    backend_value=got,
                    effective=want,
                )
            return Assessment(
                requested=want,
                support=Support.DEGRADED,
                reason="conversion-disabled",
                consequence=(
                    f"the device scans '{got}' and software conversion is "
                    "off (--lineart-threshold 0); output stays gray"
                ),
                backend_value=got,
                effective=fallback,
            )
        consequence = {
            ("gray", "color"): "color output; larger files",
            ("gray", "lineart"): "1-bit output; shades of gray will be lost",
            ("color", "gray"): "color will be lost",
            ("color", "lineart"): "color and shades of gray will be lost",
        }[(base, fallback)]
        return Assessment(
            requested=want,
            support=Support.DEGRADED,
            reason=f"no-{base}-mode",
            consequence=consequence,
            backend_value=got,
            effective=fallback,
        )
    return Assessment(
        requested=want,
        support=Support.UNSUPPORTED,
        reason="no-matching-mode",
        consequence=(
            f"device has no mode matching '{base}'; available: {', '.join(choices)}"
        ),
    )


def _assess_depth(mode: Assessment) -> Assessment:
    """The internal acquisition depth implied by the negotiated mode."""
    one_bit_out = mode.effective in ("lineart", "lineart-auto")
    requested = "1" if mode.requested in ("lineart", "lineart-auto") else "8"
    if mode.support is Support.UNKNOWN:
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="follows-mode",
            effective=requested,
        )
    if mode.support is Support.EMULATED:
        return Assessment(
            requested="1",
            support=Support.EMULATED,
            reason="software-1bit",
            consequence="acquired at 8 bit, reduced to 1 bit in software",
            effective="1",
        )
    return Assessment(
        requested=requested,
        support=mode.support
        if mode.support in (Support.NATIVE, Support.UNSUPPORTED)
        else Support.DEGRADED,
        reason="follows-mode",
        effective="1" if one_bit_out else "8",
    )


def assess_resolution(
    caps: dict[str, Capability] | None, resolution: int
) -> Assessment:
    """Negotiate the dpi: snapping or clamping degrades, but stays usable."""
    requested = str(resolution)
    if caps is None:
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="probe-failed",
            effective=requested,
        )
    if active_capability(caps, "resolution") is None:
        inactive = caps.get("resolution") is not None
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="resolution-option-inactive" if inactive else "no-resolution-option",
            effective=requested,
        )
    snapped = snap_resolution(resolution, caps)
    if snapped is None or snapped == resolution:
        return Assessment(
            requested=requested,
            support=Support.NATIVE,
            reason="native-resolution",
            backend_value=requested,
            effective=requested,
        )
    return Assessment(
        requested=requested,
        support=Support.DEGRADED,
        reason="resolution-snapped",
        consequence=f"scanned at {snapped} dpi instead of {resolution} dpi",
        backend_value=str(snapped),
        effective=str(snapped),
    )


def negotiate(
    caps: dict[str, Capability] | None,
    *,
    source: str,
    mode: str,
    resolution: int,
    lineart_threshold: LineartThreshold = 0.5,
) -> Plan:
    """Assess one scan request against a capability snapshot.

    Pure: ``caps`` is a parsed snapshot (``None`` when probing failed, which
    yields UNKNOWN throughout). ``mode`` accepts the engine modes plus
    ``lineart-auto``; a ``lineart`` request with ``lineart_threshold`` set
    to ``"auto"`` is normalized to it.
    """
    if mode == "lineart" and lineart_threshold == "auto":
        mode = "lineart-auto"
    mode_assessment = assess_mode(caps, mode, lineart_threshold)
    return Plan(
        source=assess_source(caps, source),
        mode=mode_assessment,
        depth=_assess_depth(mode_assessment),
        resolution=assess_resolution(caps, resolution),
    )


def log_notices(plan: Plan, logger: logging.Logger) -> None:
    """Log each selected-plan notice once.

    DEGRADED paths warn and name the consequence; EMULATED paths inform
    (the requested semantics are preserved); UNKNOWN stays at debug, since
    best-effort behavior is the documented contract there. NATIVE is silent
    and UNSUPPORTED raises before this point.
    """
    seen: set[tuple[int, str]] = set()
    for assessment in (plan.source, plan.mode, plan.resolution):
        if assessment.support is Support.DEGRADED:
            level = logging.WARNING
            message = (
                f"no exact '{assessment.requested}' support"
                f"{f'; using {assessment.backend_value!r}' if assessment.backend_value else ''}"
                f": {assessment.consequence}"
            )
        elif assessment.support is Support.EMULATED:
            level = logging.INFO
            message = assessment.consequence
        elif assessment.support is Support.UNKNOWN:
            level = logging.DEBUG
            message = (
                f"'{assessment.requested}': {assessment.reason}; continuing best-effort"
            )
        else:
            continue
        entry = (level, message)
        if entry in seen:
            continue
        seen.add(entry)
        logger.log(level, "%s", message)


def require_supported(plan: Plan) -> None:
    """Raise the established DeviceError for UNSUPPORTED settings."""
    for assessment in (plan.source, plan.mode):
        if assessment.support is Support.UNSUPPORTED:
            raise DeviceError(assessment.consequence)


@dataclass(frozen=True)
class ChoiceSupport:
    """Support per selectable GUI choice, derived from one snapshot."""

    sources: dict[str, Support]
    modes: dict[str, Support]


def choice_support(caps: dict[str, Capability] | None) -> ChoiceSupport:
    """Assess every source and mode choice a frontend offers.

    Advisory: frontends use this to gray out choices; the authoritative
    negotiation happens again immediately before every scan.
    """
    return ChoiceSupport(
        sources={
            value: assess_source(caps, value).support
            for value in ("flatbed", "adf", "adf-duplex", "adf-back")
        },
        modes={
            value: assess_mode(caps, value).support
            for value in ("lineart", "gray", "color", "lineart-auto")
        },
    )


def probe_snapshot(
    device: str,
    settings: Sequence[tuple[str, str]] = (),
    timeout_seconds: float = ADVISORY_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Capability] | None:
    """An advisory capability probe: failure and timeout become ``None``.

    ``None`` feeds :func:`negotiate`/:func:`choice_support` as UNKNOWN
    evidence. Scan-time probing keeps using
    :func:`~scanmole.options.probe_capabilities` directly, where failure is
    an error.
    """
    try:
        return probe_capabilities(device, settings, timeout_seconds)
    except (DeviceError, subprocess.SubprocessError, OSError) as exc:
        LOGGER.debug("advisory probe of %s failed: %s", device, exc)
        return None
