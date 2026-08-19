"""Map ScanMole's abstract options onto a device's actual ``scanimage`` options.

Backends describe their capabilities differently (the ``fujitsu`` backend's
``ADF Duplex`` vs. Brother's longer strings), so every option is discovered
from ``scanimage -A`` and fuzzy-matched rather than hardcoded.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from scanmole.config import PAGE_SIZES
from scanmole.errors import DeviceError, InputError
from scanmole.external import PROBE_TIMEOUT_SECONDS, run_command

LOGGER = logging.getLogger(__name__)

CapabilityKind = Literal["bool", "enum", "range", "other"]

_OPTION_LINE = re.compile(r"^ {1,8}--?([a-zA-Z][a-zA-Z0-9-]*)(.*)$")
_RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)")
_STEP = re.compile(r"\(in steps of (\d+(?:\.\d+)?)\)")

# Trailing markers that qualify an option rather than stating its value.
_QUALIFIER_MARKERS = ("advanced", "hardware", "read-only", "default")

_MODE_PREDICATES: dict[str, list[Callable[[str], bool]]] = {
    "lineart": [
        lambda c: c == "lineart",
        lambda c: "lineart" in c,
        lambda c: "black & white" in c or "black and white" in c or c == "b&w",
        lambda c: "binary" in c or c == "mono" or "monochrome" in c,
    ],
    "gray": [
        lambda c: c in ("gray", "grey", "grayscale", "greyscale"),
        lambda c: "true gray" in c or "true grey" in c,
        lambda c: ("gray" in c or "grey" in c) and "diffusion" not in c,
        lambda c: "gray" in c or "grey" in c,
    ],
    "color": [
        lambda c: c in ("color", "colour"),
        lambda c: ("color" in c or "colour" in c) and "fast" not in c,
        lambda c: "color" in c or "colour" in c,
    ],
}

# When a device lacks the requested mode, degrade rather than fail: airscan/eSCL
# devices often offer only Color and Gray, so a lineart request becomes gray.
_MODE_FALLBACKS: dict[str, list[str]] = {
    "lineart": ["gray", "color"],
    "gray": ["color", "lineart"],
    "color": ["gray", "lineart"],
}


@dataclass
class Capability:
    """A single ``scanimage`` option discovered from ``-A``.

    ``active=False`` preserves options the backend lists as ``[inactive]``
    (not settable in the device's current state). They are evidence for
    capability negotiation, which must distinguish inactive from absent, but
    command construction never passes them. ``current`` is the option's
    value from its trailing bracket marker (``[600]``, ``[ADF Front]``),
    ``step`` the increment of a stepped range (``(in steps of 100)``);
    both feed the effective-resolution policy and grid snapping.
    """

    kind: CapabilityKind = "other"
    choices: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    active: bool = True
    current: str | None = None
    step: float | None = None


def active_capability(caps: dict[str, Capability], name: str) -> Capability | None:
    """The named capability if it is present and currently settable."""
    capability = caps.get(name)
    if capability is None or not capability.active:
        return None
    return capability


def _take_marker(spec: str) -> tuple[str, str | None]:
    """Split one trailing ``[...]`` marker off an option spec.

    ``rfind`` copes with choices that themselves contain brackets, such as
    ``24bit Color[Fast]``.

    Returns:
        The spec without the marker, and the marker's content (``None``
        when the spec carries no trailing marker).
    """
    if not spec.endswith("]"):
        return spec, None
    bracket = spec.rfind(" [")
    if bracket != -1:
        return spec[:bracket].strip(), spec[bracket + 2 : -1]
    if spec.startswith("[") and "yes|no" not in spec:
        return "", spec[1:-1] or None
    return spec, None


def probe_capabilities(
    device: str,
    settings: Sequence[tuple[str, str]] = (),
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> dict[str, Capability]:
    """Parse ``scanimage -d DEV -A`` into a capability per option name.

    Backends advertise option constraints relative to the currently applied
    settings (eSCL devices report a different scan window per source, mode
    choices can depend on the source), so ``settings`` applies ordered
    ``(option, value)`` pairs, as argv entries, before the listing is read.

    Raises:
        DeviceError: If the device cannot be queried.
    """
    command = ["scanimage", "-d", device]
    for option, value in settings:
        command += [option, value]
    command.append("-A")
    try:
        result = run_command(
            command,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"timed out probing options of {device}") from exc
    if result.returncode != 0 and "--" not in result.stdout:
        detail = "\n".join(
            line
            for line in result.stderr.strip().splitlines()
            if "Output format is not set" not in line
        )
        raise DeviceError(
            f"cannot query device {device}: "
            f"{detail or f'scanimage -A exited {result.returncode}'}"
        )
    caps = parse_capabilities(result.stdout)
    if not caps:
        LOGGER.warning(
            "could not parse any options from `scanimage -d %s -A`; "
            "passing only resolution",
            device,
        )
    return caps


def parse_capabilities(listing: str) -> dict[str, Capability]:
    """Parse the text of a ``scanimage -A`` option listing.

    Pure function over the captured listing, so backend formats can be pinned
    with fixtures (tests/fixtures/scanimage-A/) without hardware.
    """
    caps: dict[str, Capability] = {}
    for line in listing.splitlines():
        match = _OPTION_LINE.match(line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2).strip()
        capability = Capability()
        if rest.endswith("[inactive]"):
            # Not settable in the device's current state (e.g. the epson2
            # backend lists "--source Flatbed [inactive]"). Preserved as
            # evidence with active=False; never passed to scanimage.
            capability.active = False
            rest = rest[: -len("[inactive]")].strip()
        # Peel trailing markers: qualifiers first ([advanced], [hardware]),
        # then the option's current value ([600], [ADF Front]).
        spec, marker = _take_marker(rest)
        while marker in _QUALIFIER_MARKERS:
            spec, marker = _take_marker(spec)
        if marker:
            capability.current = marker
        found_step = _STEP.search(spec)
        if found_step is not None:
            capability.step = float(found_step.group(1))
        if "yes|no" in rest:
            capability.kind = "bool"
        elif "|" in spec:
            capability.kind = "enum"
            capability.choices = [c.strip() for c in spec.split("|") if c.strip()]
        else:
            span = _RANGE.search(spec)
            if span:
                capability.kind = "range"
                capability.minimum = float(span.group(1))
                capability.maximum = float(span.group(2))
            elif spec:
                # A single fixed choice (e.g. "--source Flatbed [Flatbed]"):
                # still an enum, or the mapper would treat the option as absent
                # and lose e.g. the flatbed detection.
                capability.kind = "enum"
                capability.choices = [spec]
        caps[name] = capability
    return caps


def _pick(
    choices: Sequence[str], predicates: Sequence[Callable[[str], bool]]
) -> str | None:
    """Return the first choice matching the earliest predicate (lowercased)."""
    for predicate in predicates:
        for choice in choices:
            if predicate(choice.lower()):
                return choice
    return None


def _is_feeder(choice: str) -> bool:
    return "adf" in choice or "feeder" in choice or "automatic document" in choice


_SOURCE_PREDICATES: dict[str, list[Callable[[str], bool]]] = {
    "flatbed": [
        lambda c: "flatbed" in c.replace(" ", ""),
        lambda c: "platen" in c or "document table" in c,
    ],
    "adf-duplex": [
        lambda c: "duplex" in c and _is_feeder(c),
        lambda c: "duplex" in c,
    ],
    "adf-back": [
        lambda c: "back" in c and "duplex" not in c,
        lambda c: "back" in c,
    ],
    "adf": [  # front / simplex
        lambda c: (
            _is_feeder(c) and "duplex" not in c and "back" not in c and "front" in c
        ),
        lambda c: _is_feeder(c) and "duplex" not in c and "back" not in c,
        lambda c: _is_feeder(c) and "back" not in c,
    ],
}

# When a device lacks the requested source, degrade rather than fail: a duplex
# request falls back to a simplex feeder (only the backsides are lost), feeder
# requests fall back to the flatbed on flatbed-only devices. A flatbed request
# never degrades to a feeder, which would pull paper the user did not put in.
_SOURCE_FALLBACKS: dict[str, list[str]] = {
    "adf-duplex": ["adf", "flatbed"],
    "adf": ["flatbed"],
    "adf-back": ["adf", "flatbed"],
    "flatbed": [],
}


def is_flatbed_source(choice: str) -> bool:
    """Whether a backend source string denotes the flatbed/platen."""
    lowered = choice.lower()
    return any(predicate(lowered) for predicate in _SOURCE_PREDICATES["flatbed"])


def map_source(want: str, caps: dict[str, Capability]) -> str | None:
    """Map ``adf-duplex``/``adf``/``adf-back``/``flatbed`` onto ``--source``.

    Degrades to a related source when the requested one is absent (see
    ``_SOURCE_FALLBACKS``). Pure selection: the capability negotiation layer
    owns user-facing fallback notices.

    Returns:
        The device's matching source string, or ``None`` when the device has no
        ``--source`` option.

    Raises:
        DeviceError: If no source (requested or fallback) is available.
    """
    capability = active_capability(caps, "source")
    if capability is None or not capability.choices:
        LOGGER.debug("device has no active --source option; not passing one")
        return None
    choices = capability.choices
    for attempt in [want, *_SOURCE_FALLBACKS[want]]:
        got = _pick(choices, _SOURCE_PREDICATES[attempt])
        if got is not None:
            LOGGER.debug("source '%s' -> '%s'", want, got)
            return got
    raise DeviceError(
        f"device has no source matching '{want}'; available: {', '.join(choices)}"
    )


def map_mode(want: str, caps: dict[str, Capability]) -> str | None:
    """Map ``lineart``/``gray``/``color`` onto the device's ``--mode`` choices.

    Degrades to a related mode when the requested one is absent. Pure
    selection: the capability negotiation layer owns user-facing notices.

    Returns:
        The device's matching mode string, or ``None`` when the device has no
        ``--mode`` option.

    Raises:
        DeviceError: If no mode (requested or fallback) is available.
    """
    capability = active_capability(caps, "mode")
    if capability is None or not capability.choices:
        LOGGER.debug("device has no active --mode option; not passing one")
        return None
    choices = capability.choices
    for attempt in [want, *_MODE_FALLBACKS[want]]:
        got = _pick(choices, _MODE_PREDICATES[attempt])
        if got is not None:
            LOGGER.debug("mode '%s' -> '%s'", want, got)
            return got
    raise DeviceError(
        f"device has no mode matching '{want}'; available: {', '.join(choices)}"
    )


def parse_page_size(spec: str) -> tuple[float, float] | None:
    """Parse ``a4``/``letter``/..., ``WxH`` (mm) or ``auto`` into a size.

    Returns:
        The width/height pair in millimetres, or ``None`` for ``auto``: the
        scan then uses the device's maximum window and the pipeline crops each
        page to the detected paper edges.

    Raises:
        InputError: If ``spec`` is neither ``auto``, a known name nor ``WxH``
            in mm.
    """
    text = spec.strip().lower()
    if text == "auto":
        return None
    if text in PAGE_SIZES:
        return PAGE_SIZES[text]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", text)
    if match is None:
        raise InputError(
            f"invalid --page-size '{spec}' "
            "(use auto, a4|a5|a6|letter|legal, or WxH in mm)"
        )
    width, height = float(match.group(1)), float(match.group(2))
    if width <= 0 or height <= 0:
        raise InputError(f"invalid --page-size '{spec}' (dimensions must be > 0 mm)")
    return width, height


def _snap_to_step(value: float, capability: Capability) -> float:
    """Snap a finite value to the range's grid, anchored at its minimum.

    Ties break deterministically to the lower grid point. Values are
    clamped to the advertised limits afterwards by the callers, so a snap
    can never escape the range.
    """
    step = capability.step
    if step is None or step <= 0 or not math.isfinite(value):
        return value
    minimum = capability.minimum if capability.minimum is not None else 0.0
    below = minimum + math.floor((value - minimum) / step) * step
    above = below + step
    if capability.maximum is not None and above > capability.maximum:
        return below
    if value - below <= above - value:  # tie -> lower
        return below
    return above


def format_mm(value: float, capability: Capability | None, option: str) -> str:
    """Clamp a millimetre value to the range's grid and format it tersely.

    Stepped ranges are snapped to their advertised grid first (anchored at
    the range minimum, ties to the lower point), then clamped, so emitted
    scan-window values are ones the backend can actually apply.
    """
    clamped = value
    if capability is not None and capability.kind == "range":
        clamped = _snap_to_step(clamped, capability)
        if capability.maximum is not None and clamped > capability.maximum:
            LOGGER.debug(
                "clamping %s %g -> %gmm (device maximum)",
                option,
                clamped,
                capability.maximum,
            )
            clamped = capability.maximum
        if capability.minimum is not None and clamped < capability.minimum:
            clamped = capability.minimum
    return f"{clamped:g}"


def snap_resolution(resolution: int, caps: dict[str, Capability]) -> int | None:
    """Adjust a requested dpi to what the device actually offers.

    Returns:
        The dpi to request, or ``None`` when the device has no ``--resolution``
        option (so it should not be passed at all).
    """
    capability = active_capability(caps, "resolution")
    if capability is None:
        return None
    if capability.kind == "enum":
        values = sorted(
            {
                int(found.group())
                for choice in capability.choices
                if (found := re.search(r"\d+", choice)) is not None
            }
        )
        if values and resolution not in values:
            return min(values, key=lambda value: (abs(value - resolution), value))
    elif capability.kind == "range" and capability.maximum is not None:
        if resolution > capability.maximum:
            return int(capability.maximum)
        if capability.minimum is not None and resolution < capability.minimum:
            return int(capability.minimum)
        snapped = _snap_to_step(float(resolution), capability)
        if snapped != resolution:
            return round(snapped)
    return resolution
