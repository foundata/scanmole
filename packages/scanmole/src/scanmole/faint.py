"""Negotiate ``lineart-auto``: faint originals, and who rescues them.

The request is "1-bit output that keeps faint pencil, carbon copies and
worn thermal print readable", which two very different paths can serve. A
scanner with its own text enhancement (Epson TET, Fujitsu SDTC) does it in
hardware, but SANE option activity is state-dependent, so recognizing one
takes a staged set-and-reprobe rather than a look at a listing. Everything
else is served by acquiring gray or color and applying ScanMole's guarded
adaptive threshold, which is why the acquisition depth follows the mode and
is decided here too.

Matching is evidence-based and fixture-pinned: active option topology only,
never device identities. Anything short of proof falls back to the
information-preserving software path.
"""

from __future__ import annotations

import re
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
from scanmole.options import (
    _MODE_PREDICATES,
    Capability,
    _pick,
    readable_capability,
    writable_capability,
)

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
    halftoning = writable_capability(caps, "halftoning")
    if halftoning is not None and _TET_CHOICE in halftoning.choices:
        return NativeEnhancement(
            reason="native-epson-tet",
            notice=(
                "using the scanner's built-in text enhancement "
                "(Text Enhanced Technology)"
            ),
            settings=(("--halftoning", _TET_CHOICE),),
        )
    threshold = writable_capability(caps, "threshold")
    variance = writable_capability(caps, "variance")
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
    return _engaged_enhancement(caps)


def _engaged_enhancement(caps: dict[str, Capability]) -> NativeEnhancement | None:
    """A read-only enhancement whose current values prove it is already on.

    Read-only controls cannot be configured, so the only way one counts is
    if the device already reports the enhancement engaged. Nothing is
    emitted for it; the settings tuple stays empty.
    """
    halftoning = readable_capability(caps, "halftoning")
    if (
        halftoning is not None
        and not halftoning.settable
        and (halftoning.current or "").strip() == _TET_CHOICE
    ):
        return NativeEnhancement(
            reason="native-epson-tet",
            notice=(
                "the scanner's built-in text enhancement (Text Enhanced "
                "Technology) is already engaged"
            ),
            settings=(),
        )
    threshold = readable_capability(caps, "threshold")
    variance = readable_capability(caps, "variance")
    if (
        threshold is not None
        and variance is not None
        and not threshold.settable
        and (threshold.current or "").strip() in ("0", "0.0")
    ):
        return NativeEnhancement(
            reason="native-fujitsu-sdtc",
            notice="the scanner's built-in text enhancement (SDTC) is already engaged",
            settings=(),
        )
    return None


@dataclass(frozen=True)
class _LineartCandidate:
    """The device's own 1-bit mode and what it takes to get there.

    ``effective`` is the mode the scan runs in. ``backend_value`` is what
    the command may emit for it, which is ``None`` when the device already
    sits in that mode and will not accept a value. ``settings`` are the
    ordered options the staged probe applies on top of the base settings,
    and are empty for the same reason.
    """

    effective: str
    backend_value: str | None
    settings: Settings


def _native_lineart_candidate(
    caps: dict[str, Capability] | None,
) -> _LineartCandidate | None:
    """The device's own 1-bit mode, the candidate for enhancement.

    A writable ``--mode`` offering a 1-bit choice is set and reprobed. A
    read-only ``--mode`` whose current value is already a 1-bit mode is
    just as much a 1-bit scan and just as eligible for a native
    enhancement; it simply cannot be set, so it contributes nothing to
    emit and the probe reads the state as it stands.
    """
    if caps is None:
        return None
    capability = readable_capability(caps, "mode")
    if capability is None:
        return None
    choice = _pick(state_choices(capability), _MODE_PREDICATES["lineart"])
    if choice is None:
        return None
    if not capability.settable:
        return _LineartCandidate(choice, None, ())
    return _LineartCandidate(choice, choice, (("--mode", choice),))


def _native_faint_assessment(
    candidate: _LineartCandidate, enhancement: NativeEnhancement
) -> Assessment:
    return Assessment(
        requested="lineart-auto",
        support=Support.NATIVE,
        reason=enhancement.reason,
        consequence=enhancement.notice,
        backend_value=candidate.backend_value,
        actual=candidate.effective,
        effective="lineart-auto",
    )


def software_faint(caps: dict[str, Capability] | None) -> Assessment:
    """The information-preserving software path for ``lineart-auto``.

    Prefers Gray, then Color, both converted by the guarded adaptive
    threshold. A device that conclusively offers only ordinary 1-bit modes
    is UNSUPPORTED: an unenhanced 1-bit scan cannot preserve the faint
    shades the request is about, which is a failure to deliver, not a
    warnable degradation. A read-only mode is state rather than a choice,
    so the same rules run against its current value alone and nothing is
    emitted for it: a device parked in Gray can still serve the request,
    and one parked in plain 1-bit conclusively cannot. Missing or inactive
    evidence stays UNKNOWN (best-effort; the pipeline still refuses an
    unenhanced 1-bit result).
    """
    if caps is None:
        return Assessment(
            requested="lineart-auto",
            support=Support.UNKNOWN,
            reason="probe-failed",
            consequence="capabilities could not be read; trying as requested",
            effective="lineart-auto",
        )
    capability = readable_capability(caps, "mode")
    choices = state_choices(capability)
    if capability is None or not choices:
        inactive = caps.get("mode") is not None
        return Assessment(
            requested="lineart-auto",
            support=Support.UNKNOWN,
            reason="mode-option-inactive" if inactive else "no-mode-option",
            consequence="the device does not advertise usable modes; "
            "trying as requested",
            effective="lineart-auto",
        )
    if not capability.settable:
        return as_read_only(_matchsoftware_faint(choices))
    return as_written(_matchsoftware_faint(choices))


def _matchsoftware_faint(choices: list[str]) -> Assessment:
    """Match ``lineart-auto`` against the modes a device can deliver."""
    for fallback, reason in (("gray", "adaptive-gray"), ("color", "adaptive-color")):
        got = _pick(choices, _MODE_PREDICATES[fallback])
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
                actual=got,
                effective="lineart-auto",
            )
    if _pick(choices, _MODE_PREDICATES["lineart"]) is not None:
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
            f"device has no mode matching 'lineart'; available: {', '.join(choices)}"
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
    candidate = _native_lineart_candidate(caps)
    if caps is not None and candidate is not None:
        enhancement = detect_native_enhancement(caps)
        if enhancement is not None:
            return _native_faint_assessment(candidate, enhancement)
    return software_faint(caps)


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
        depth=assess_depth(assessment, caps),
        resolution=plan.resolution,
        extra_options=extra,
    )


def _resolve_faint_mode(
    caps: dict[str, Capability] | None,
    prober: Prober,
    base_settings: Settings,
) -> tuple[Assessment, Settings]:
    candidate = _native_lineart_candidate(caps)
    if candidate is not None:
        # A read-only candidate contributes no settings, so the reprobe
        # sees the base state: exactly the snapshot the scan will run in.
        applied = (*base_settings, *candidate.settings)
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
    return software_faint(caps), ()


def _enhancement_verified(
    enhancement: NativeEnhancement, applied: Settings, prober: Prober
) -> bool:
    if enhancement.verify_option is None:
        return True
    verified = prober((*applied, *enhancement.settings))
    return (
        verified is not None
        and readable_capability(verified, enhancement.verify_option) is not None
    )


def _eight_bit_choice(caps: dict[str, Capability] | None) -> str | None:
    """The value engaging an explicit 8-bit depth, if the device has one."""
    if caps is None:
        return None
    capability = writable_capability(caps, "depth")
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


def assess_depth(
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
