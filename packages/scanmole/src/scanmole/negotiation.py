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
import re
import subprocess
from collections.abc import Callable, Sequence
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
    parse_dpi,
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
    ``--lineart-threshold auto``). The ``lineart-auto`` verdict here is the
    command-safe software view; a native enhancement path is only ever
    claimed by the staged :func:`resolve_faint_plan` (authoritative) or the
    optimistic :func:`advisory_faint_assessment` (display only).
    """
    if want == "lineart-auto":
        return _software_faint(caps)
    base = want
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


Settings = tuple[tuple[str, str], ...]

Prober = Callable[[Settings], "dict[str, Capability] | None"]
"""A capability probe with ordered settings applied; failure returns None."""

_TET_CHOICE = "Text Enhanced Technology"


@dataclass(frozen=True)
class NativeEnhancement:
    """An evidence-backed native faint-text enhancement path.

    ``settings`` are the ordered backend options that engage the
    enhancement, beyond selecting the 1-bit mode itself. ``verify_option``,
    when set, must still be active after a reprobe with the complete
    ordered settings applied, or the path is rejected.
    """

    reason: str
    notice: str
    settings: Settings
    verify_option: str | None = None


def detect_native_enhancement(
    caps: dict[str, Capability],
) -> NativeEnhancement | None:
    """Recognize a native faint-text enhancement in a 1-bit-mode snapshot.

    Matches the active option topology only, never device identities, and
    only profiles with fixture-backed evidence:

    - Epson TET: an active ``--halftoning`` whose choices contain exactly
      ``Text Enhanced Technology`` (background filtering plus dynamic
      thresholding in the scanner).
    - Fujitsu SDTC: an active ``--threshold`` range containing 0 (0 selects
      the automatic DTC circuit) together with an active ``--variance``
      (the SDTC sensitivity, where 0 is the documented default).

    Deliberately not evidence: generic threshold/brightness/contrast
    controls, halftone or error-diffusion *mode choices* (Canon
    ``Halftone``, Brother ``Gray[Error Diffusion]``), ``threshold-curve``
    style controls, and any inactive option.
    """
    halftoning = active_capability(caps, "halftoning")
    if halftoning is not None and _TET_CHOICE in halftoning.choices:
        return NativeEnhancement(
            reason="native-epson-tet",
            notice=(
                "using the scanner's built-in text enhancement "
                "(Text Enhanced Technology)"
            ),
            settings=(("--halftoning", _TET_CHOICE),),
        )
    threshold = active_capability(caps, "threshold")
    variance = active_capability(caps, "variance")
    if (
        variance is not None
        and threshold is not None
        and threshold.kind == "range"
        and threshold.minimum is not None
        and threshold.maximum is not None
        and threshold.minimum <= 0 <= threshold.maximum
    ):
        return NativeEnhancement(
            reason="native-fujitsu-sdtc",
            notice="using the scanner's built-in text enhancement (SDTC)",
            settings=(("--threshold", "0"), ("--variance", "0")),
            verify_option="variance",
        )
    return None


def _native_lineart_choice(caps: dict[str, Capability] | None) -> str | None:
    """The device's own 1-bit mode choice, the candidate for enhancement."""
    if caps is None:
        return None
    capability = active_capability(caps, "mode")
    if capability is None or not capability.choices:
        return None
    return _pick(capability.choices, _MODE_PREDICATES["lineart"])


def _native_faint_assessment(
    candidate: str, enhancement: NativeEnhancement
) -> Assessment:
    return Assessment(
        requested="lineart-auto",
        support=Support.NATIVE,
        reason=enhancement.reason,
        consequence=enhancement.notice,
        backend_value=candidate,
        effective="lineart-auto",
    )


def _software_faint(caps: dict[str, Capability] | None) -> Assessment:
    """The information-preserving software path for ``lineart-auto``.

    Prefers Gray, then Color, both converted by the guarded adaptive
    threshold. A device that conclusively offers only ordinary 1-bit modes
    is UNSUPPORTED: an unenhanced 1-bit scan cannot preserve the faint
    shades the request is about, which is a failure to deliver, not a
    warnable degradation. Missing or inactive evidence stays UNKNOWN
    (best-effort; the pipeline still refuses an unenhanced 1-bit result).
    """
    if caps is None:
        return Assessment(
            requested="lineart-auto",
            support=Support.UNKNOWN,
            reason="probe-failed",
            consequence="capabilities could not be read; trying as requested",
            effective="lineart-auto",
        )
    capability = active_capability(caps, "mode")
    if capability is None or not capability.choices:
        inactive = caps.get("mode") is not None
        return Assessment(
            requested="lineart-auto",
            support=Support.UNKNOWN,
            reason="mode-option-inactive" if inactive else "no-mode-option",
            consequence="the device does not advertise usable modes; "
            "trying as requested",
            effective="lineart-auto",
        )
    for fallback, reason in (("gray", "adaptive-gray"), ("color", "adaptive-color")):
        got = _pick(capability.choices, _MODE_PREDICATES[fallback])
        if got is not None:
            return Assessment(
                requested="lineart-auto",
                support=Support.EMULATED,
                reason=reason,
                consequence=(
                    f"the device scans '{got}'; ScanMole applies the guarded "
                    "faint-originals threshold in software"
                ),
                backend_value=got,
                effective="lineart-auto",
            )
    if _pick(capability.choices, _MODE_PREDICATES["lineart"]) is not None:
        return Assessment(
            requested="lineart-auto",
            support=Support.UNSUPPORTED,
            reason="no-information-preserving-path",
            consequence=(
                "the device offers only plain 1-bit scanning, which cannot "
                "preserve faint shades; select the ordinary B/W mode "
                "(a numeric --lineart-threshold) instead"
            ),
        )
    return Assessment(
        requested="lineart-auto",
        support=Support.UNSUPPORTED,
        reason="no-matching-mode",
        consequence=(
            "device has no mode matching 'lineart'; "
            f"available: {', '.join(capability.choices)}"
        ),
    )


def advisory_faint_assessment(caps: dict[str, Capability] | None) -> Assessment:
    """A frontend's optimistic ``lineart-auto`` verdict from one snapshot.

    A native enhancement signature visible in the snapshot makes the choice
    tentatively NATIVE, pending the scan-time set-and-reprobe confirmation;
    otherwise the software verdict applies. Display only: command
    construction never uses this (a tentative claim without the staged
    settings would emit plain 1-bit lineart, the exact bug the faint mode
    exists to avoid).
    """
    candidate = _native_lineart_choice(caps)
    if caps is not None and candidate is not None:
        enhancement = detect_native_enhancement(caps)
        if enhancement is not None:
            return _native_faint_assessment(candidate, enhancement)
    return _software_faint(caps)


def resolve_faint_plan(
    plan: Plan,
    caps: dict[str, Capability] | None,
    prober: Prober,
    base_settings: Settings = (),
) -> Plan:
    """Resolve a ``lineart-auto`` plan through staged set-and-reprobe.

    SANE option activity is state-dependent, so native recognition applies
    the candidate 1-bit mode on top of ``base_settings`` (normally the
    negotiated source) and classifies the reprobed snapshot; the Fujitsu
    SDTC profile additionally requires ``--variance`` to stay active with
    the complete ordered settings applied. Any failed or rejected probe
    falls back to the information-preserving software path. ``prober``
    performs the I/O; classification stays pure over the snapshots.
    """
    assessment, extra = _resolve_faint_mode(caps, prober, base_settings)
    return Plan(
        source=plan.source,
        mode=assessment,
        depth=_assess_depth(assessment, caps),
        resolution=plan.resolution,
        extra_options=extra,
    )


def _resolve_faint_mode(
    caps: dict[str, Capability] | None,
    prober: Prober,
    base_settings: Settings,
) -> tuple[Assessment, Settings]:
    candidate = _native_lineart_choice(caps)
    if candidate is not None:
        applied = (*base_settings, ("--mode", candidate))
        staged = prober(applied)
        if staged is not None:
            enhancement = detect_native_enhancement(staged)
            if enhancement is not None and _enhancement_verified(
                enhancement, applied, prober
            ):
                return (
                    _native_faint_assessment(candidate, enhancement),
                    enhancement.settings,
                )
    return _software_faint(caps), ()


def _enhancement_verified(
    enhancement: NativeEnhancement, applied: Settings, prober: Prober
) -> bool:
    if enhancement.verify_option is None:
        return True
    verified = prober((*applied, *enhancement.settings))
    return (
        verified is not None
        and active_capability(verified, enhancement.verify_option) is not None
    )


def _eight_bit_choice(caps: dict[str, Capability] | None) -> str | None:
    """The value engaging an explicit 8-bit depth, if the device has one."""
    if caps is None:
        return None
    capability = active_capability(caps, "depth")
    if capability is None:
        return None
    if capability.kind == "enum":
        for choice in capability.choices:
            found = re.search(r"\d+", choice)
            if found is not None and int(found.group()) == 8:
                return "8"
        return None
    if (
        capability.kind == "range"
        and capability.minimum is not None
        and capability.maximum is not None
        and capability.minimum <= 8 <= capability.maximum
    ):
        return "8"
    return None


def _assess_depth(
    mode: Assessment, caps: dict[str, Capability] | None = None
) -> Assessment:
    """The internal acquisition depth implied by the negotiated mode.

    The adaptive faint path pins an explicit 8-bit depth where the device
    exposes an active one: the guarded threshold needs true 8-bit
    brightness data, so the backend must not fall back to a 1-bit or
    16-bit delivery. The value is carried as ``backend_value`` and emitted
    by the scan command.
    """
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
        adaptive = mode.reason in ("adaptive-gray", "adaptive-color")
        return Assessment(
            requested="1",
            support=Support.EMULATED,
            reason="software-1bit",
            consequence="acquired at 8 bit, reduced to 1 bit in software",
            backend_value=_eight_bit_choice(caps) if adaptive else None,
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
        depth=_assess_depth(mode_assessment, caps),
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
