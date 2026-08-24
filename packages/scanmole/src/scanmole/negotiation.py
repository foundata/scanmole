"""Capability negotiation: what a device supports, and how well.

Assesses a scan request (source, mode, resolution) against a capability
snapshot and returns a plan with structured notices. Pure: probing I/O has
a thin helper, everything else takes data in and returns verdicts out. The
vocabulary those verdicts are written in lives in
:mod:`scanmole.assessment`, and the staged ``lineart-auto`` recognition in
:mod:`scanmole.faint`; both are re-exported here, because this module is
the negotiation layer's front door for the engine and the frontends alike.

This models ScanMole's workflows (sources, modes, acquisition depth,
resolution), not arbitrary SANE options, and it is deliberately not a
complete SANE frontend. Matching stays evidence-based and fixture-pinned;
there are no device identity lists. Real backends are imperfect (the
DS-730N via epson2 reports an inactive Flatbed source on a sheet-fed
device), which is why missing or inactive evidence is UNKNOWN and stays
usable best-effort, never UNSUPPORTED.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scanmole.assessment import (
    Assessment,
    Plan,
    Prober,
    Settings,
    Support,
    as_read_only,
    as_written,
    state_choices,
)
from scanmole.config import LineartThreshold
from scanmole.errors import DeviceError
from scanmole.faint import (
    NativeEnhancement,
    advisory_faint_assessment,
    assess_depth,
    detect_native_enhancement,
    resolve_faint_plan,
    software_faint,
)
from scanmole.options import (
    _MODE_FALLBACKS,
    _MODE_PREDICATES,
    _SOURCE_FALLBACKS,
    _SOURCE_PREDICATES,
    Capability,
    _pick,
    parse_dpi,
    probe_capabilities,
    readable_capability,
    snap_resolution,
)

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ADVISORY_PROBE_TIMEOUT_SECONDS",
    "Assessment",
    "ChoiceSupport",
    "NativeEnhancement",
    "Plan",
    "Prober",
    "Settings",
    "Support",
    "advisory_faint_assessment",
    "assess_depth",
    "assess_mode",
    "assess_resolution",
    "assess_source",
    "choice_support",
    "detect_native_enhancement",
    "log_notices",
    "negotiate",
    "probe_snapshot",
    "require_supported",
    "resolve_faint_plan",
]
"""The negotiation layer's public surface.

Split across three modules for readability, entered through one: callers
ask this module what a device supports without having to know whether the
answer came from the shared model, the general assessment or the staged
faint-originals recognition."""

ADVISORY_PROBE_TIMEOUT_SECONDS = 15.0
"""Timeout for advisory (GUI) capability probes.

Scan-time negotiation keeps the longer probe timeout and treats failure as
an error; an advisory probe turns timeout or failure into UNKNOWN instead,
so a slow or wedged backend cannot freeze a frontend.
"""


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
    capability = readable_capability(caps, "source")
    choices = state_choices(capability)
    if capability is None or not choices:
        inactive = caps.get("source") is not None
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="source-option-inactive" if inactive else "no-source-option",
            consequence="the device does not advertise usable sources; "
            "trying as requested",
            effective=want,
        )
    if not capability.settable:
        # Read-only: the current value is what the device will use, so the
        # ordinary matching runs against it alone and establishes effective
        # behaviour, but nothing may be emitted for it.
        return as_read_only(_match_source(choices, want))
    return as_written(_match_source(choices, want))


def _match_source(choices: list[str], want: str) -> Assessment:
    """Match a source request against the choices the device offers."""
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
    ``--lineart-threshold auto``). The ``lineart-auto`` verdict here is the
    command-safe software view; a native enhancement path is only ever
    claimed by the staged :func:`resolve_faint_plan` (authoritative) or the
    optimistic :func:`advisory_faint_assessment` (display only).
    """
    if want == "lineart-auto":
        return software_faint(caps)
    base = want
    if caps is None:
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="probe-failed",
            consequence="capabilities could not be read; trying as requested",
            effective=want,
        )
    capability = readable_capability(caps, "mode")
    choices = state_choices(capability)
    if capability is None or not choices:
        inactive = caps.get("mode") is not None
        return Assessment(
            requested=want,
            support=Support.UNKNOWN,
            reason="mode-option-inactive" if inactive else "no-mode-option",
            consequence="the device does not advertise usable modes; "
            "trying as requested",
            effective=want,
        )
    if not capability.settable:
        # Read-only: match against the current value alone and keep the
        # verdict, but never emit a value for it.
        return as_read_only(_match_mode(choices, want, base, lineart_threshold))
    return as_written(_match_mode(choices, want, base, lineart_threshold))


def _match_mode(
    choices: list[str], want: str, base: str, lineart_threshold: LineartThreshold
) -> Assessment:
    """Match a mode request against the choices the device offers."""
    native = _pick(choices, _MODE_PREDICATES[base])
    if native is not None:
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


def _singleton_resolution(capability: Capability) -> int | None:
    """The dpi of a genuinely fixed constraint, if it really is one.

    Only a single exact numeric enum choice or a range with equal bounds
    counts: an *adjustable* inactive range (``75..600dpi [75]``) states
    nothing about what the backend would use, and its current value must
    not be promoted to physical-geometry evidence.
    """
    if capability.kind == "enum" and len(capability.choices) == 1:
        return parse_dpi(capability.choices[0])
    if (
        capability.kind == "range"
        and capability.minimum is not None
        and capability.minimum == capability.maximum
    ):
        value = int(capability.minimum)
        return value if value == capability.minimum and value > 0 else None
    return None


def _numeric_resolution_evidence(capability: Capability) -> bool:
    """Whether an option's constraint is usable for snapping and emission."""
    if capability.kind == "range":
        return capability.minimum is not None and capability.maximum is not None
    if capability.kind == "enum":
        return any(parse_dpi(choice) is not None for choice in capability.choices)
    return False


def _fixed_assessment(requested: int, fixed: int) -> Assessment:
    if fixed == requested:
        return Assessment(
            requested=str(requested),
            support=Support.NATIVE,
            reason="fixed-resolution",
            effective=str(fixed),
        )
    return Assessment(
        requested=str(requested),
        support=Support.DEGRADED,
        reason="fixed-resolution",
        consequence=f"the device is fixed at {fixed} dpi instead of {requested} dpi",
        effective=str(fixed),
    )


def assess_resolution(
    caps: dict[str, Capability] | None, resolution: int
) -> Assessment:
    """Negotiate the dpi that establishes the pages' physical geometry.

    A writable, numerically parseable option is set explicitly after
    enum/range/step snapping. A read-only option with an exact numeric
    current value, or an inactive option whose constraint is genuinely
    fixed (one numeric choice, equal range bounds), establishes the
    effective dpi without emitting ``--resolution``. Everything else
    (opaque, non-numeric, or adjustable-but-inactive) stays UNKNOWN with
    an empty ``effective``: the requested dpi must never masquerade as an
    established one, because PDF page dimensions are derived from it
    (scan-time acquisition refuses to run on UNKNOWN).
    """
    requested = str(resolution)
    if caps is None:
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="probe-failed",
        )
    capability = caps.get("resolution")
    if capability is None:
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="no-resolution-option",
        )
    if not capability.active:
        # Inactive evidence counts only when the constraint is genuinely
        # fixed; the current value of an adjustable inactive range states
        # nothing about what the backend would use.
        fixed = _singleton_resolution(capability)
        if fixed is not None:
            return _fixed_assessment(resolution, fixed)
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="resolution-option-inactive",
        )
    if not capability.settable:
        # Read-only state: an exact numeric current value (or a genuinely
        # fixed constraint) establishes the dpi without emission.
        fixed = (
            parse_dpi(capability.current) if capability.current is not None else None
        )
        if fixed is None:
            fixed = _singleton_resolution(capability)
        if fixed is not None:
            return _fixed_assessment(resolution, fixed)
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="resolution-not-parseable",
        )
    if not _numeric_resolution_evidence(capability):
        # Active and writable but opaque (kind "other", a non-numeric
        # enum): emitting the request would trust the backend blindly.
        return Assessment(
            requested=requested,
            support=Support.UNKNOWN,
            reason="resolution-not-parseable",
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
        depth=assess_depth(mode_assessment, caps),
        resolution=assess_resolution(caps, resolution),
    )


def log_notices(plan: Plan, logger: logging.Logger) -> None:
    """Log each selected-plan notice once.

    DEGRADED paths warn and name the consequence; EMULATED paths inform
    (the requested semantics are preserved); UNKNOWN stays at debug, since
    best-effort behavior is the documented contract there. NATIVE is silent
    unless it carries a consequence note (a native faint-text enhancement
    names itself once), and UNSUPPORTED raises before this point.
    """
    seen: set[tuple[int, str]] = set()
    for assessment in (plan.source, plan.mode, plan.resolution):
        if assessment.support is Support.NATIVE and assessment.consequence:
            level = logging.INFO
            message = assessment.consequence
        elif assessment.support is Support.DEGRADED:
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
            value: (
                advisory_faint_assessment(caps)
                if value == "lineart-auto"
                else assess_mode(caps, value)
            ).support
            for value in ("lineart", "gray", "color", "lineart-auto")
        },
    )


def probe_snapshot(
    device: str,
    settings: Sequence[tuple[str, str]] = (),
    timeout_seconds: float = ADVISORY_PROBE_TIMEOUT_SECONDS,
    on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> dict[str, Capability] | None:
    """An advisory capability probe: failure and timeout become ``None``.

    ``None`` feeds :func:`negotiate`/:func:`choice_support` as UNKNOWN
    evidence. Scan-time probing keeps using
    :func:`~scanmole.options.probe_capabilities` directly, where failure is
    an error.
    """
    try:
        return probe_capabilities(device, settings, timeout_seconds, on_spawn)
    except (DeviceError, subprocess.SubprocessError, OSError) as exc:
        LOGGER.debug("advisory probe of %s failed: %s", device, exc)
        return None
