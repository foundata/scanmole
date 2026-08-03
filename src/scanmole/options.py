"""Map ScanMole's abstract options onto a device's actual ``scanimage`` options.

Backends describe their capabilities differently (Fujitsu's ``ADF Duplex`` vs.
Brother's longer strings), so every option is discovered from ``scanimage -A``
and fuzzy-matched rather than hardcoded.
"""

from __future__ import annotations

import logging
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
    """A single ``scanimage`` option discovered from ``-A``."""

    kind: CapabilityKind = "other"
    choices: list[str] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None


def _strip_default_marker(spec: str) -> str:
    """Drop a trailing ``[default]``/``[inactive]`` marker from an option spec.

    ``rfind`` copes with choices that themselves contain brackets, such as
    ``24bit Color[Fast]``.
    """
    if not spec.endswith("]"):
        return spec
    bracket = spec.rfind(" [")
    if bracket != -1:
        return spec[:bracket].strip()
    if spec.startswith("[") and "yes|no" not in spec:
        return ""
    return spec


def probe_capabilities(device: str) -> dict[str, Capability]:
    """Parse ``scanimage -d DEV -A`` into a capability per option name.

    Raises:
        DeviceError: If the device cannot be queried.
    """
    try:
        result = run_command(
            ["scanimage", "-d", device, "-A"],
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
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

    caps: dict[str, Capability] = {}
    for line in result.stdout.splitlines():
        match = _OPTION_LINE.match(line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2).strip()
        spec = _strip_default_marker(rest)
        capability = Capability()
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
        caps[name] = capability
    if not caps:
        LOGGER.warning(
            "could not parse any options from `scanimage -d %s -A`; "
            "passing only resolution",
            device,
        )
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


def map_source(want: str, caps: dict[str, Capability]) -> str | None:
    """Map ``adf-duplex``/``adf``/``adf-back``/``flatbed`` onto ``--source``.

    Returns:
        The device's matching source string, or ``None`` when the device has no
        ``--source`` option.

    Raises:
        DeviceError: If the device has sources but none match ``want``.
    """
    capability = caps.get("source")
    if capability is None or not capability.choices:
        LOGGER.debug("device has no --source option; not passing one")
        return None
    choices = capability.choices
    if want == "flatbed":
        got = _pick(
            choices,
            [
                lambda c: "flatbed" in c.replace(" ", ""),
                lambda c: "platen" in c or "document table" in c,
            ],
        )
    elif want == "adf-duplex":
        got = _pick(
            choices,
            [lambda c: "duplex" in c and _is_feeder(c), lambda c: "duplex" in c],
        )
    elif want == "adf-back":
        got = _pick(
            choices,
            [lambda c: "back" in c and "duplex" not in c, lambda c: "back" in c],
        )
    else:  # adf (front / simplex)
        got = _pick(
            choices,
            [
                lambda c: (
                    _is_feeder(c)
                    and "duplex" not in c
                    and "back" not in c
                    and "front" in c
                ),
                lambda c: _is_feeder(c) and "duplex" not in c and "back" not in c,
                lambda c: _is_feeder(c) and "back" not in c,
            ],
        )
    if got is None:
        raise DeviceError(
            f"device has no source matching '{want}'; available: {', '.join(choices)}"
        )
    LOGGER.debug("source '%s' -> '%s'", want, got)
    return got


def map_mode(want: str, caps: dict[str, Capability]) -> str | None:
    """Map ``lineart``/``gray``/``color`` onto the device's ``--mode`` choices.

    Degrades to a related mode with a warning when the requested one is absent.

    Returns:
        The device's matching mode string, or ``None`` when the device has no
        ``--mode`` option.

    Raises:
        DeviceError: If no mode (requested or fallback) is available.
    """
    capability = caps.get("mode")
    if capability is None or not capability.choices:
        LOGGER.debug("device has no --mode option; not passing one")
        return None
    choices = capability.choices
    for attempt in [want, *_MODE_FALLBACKS[want]]:
        got = _pick(choices, _MODE_PREDICATES[attempt])
        if got is not None:
            if attempt != want:
                LOGGER.warning(
                    "device has no '%s' mode; falling back to '%s'", want, got
                )
            else:
                LOGGER.debug("mode '%s' -> '%s'", want, got)
            return got
    raise DeviceError(
        f"device has no mode matching '{want}'; available: {', '.join(choices)}"
    )


def parse_page_size(spec: str) -> tuple[float, float]:
    """Parse ``a4``/``letter``/... or ``WxH`` (mm) into a width/height pair.

    Raises:
        InputError: If ``spec`` is neither a known name nor ``WxH`` in mm.
    """
    text = spec.strip().lower()
    if text in PAGE_SIZES:
        return PAGE_SIZES[text]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", text)
    if match is None:
        raise InputError(
            f"invalid --page-size '{spec}' (use a4|a5|a6|letter|legal or WxH in mm)"
        )
    return float(match.group(1)), float(match.group(2))


def format_mm(value: float, capability: Capability | None, option: str) -> str:
    """Clamp a millimetre value to a range capability and format it tersely."""
    clamped = value
    if capability is not None and capability.kind == "range":
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
    capability = caps.get("resolution")
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
            nearest = min(values, key=lambda value: (abs(value - resolution), value))
            LOGGER.warning(
                "device does not offer %d dpi; using %d dpi", resolution, nearest
            )
            return nearest
    elif capability.kind == "range" and capability.maximum is not None:
        if resolution > capability.maximum:
            LOGGER.warning("device maximum is %g dpi; using that", capability.maximum)
            return int(capability.maximum)
        if capability.minimum is not None and resolution < capability.minimum:
            return int(capability.minimum)
    return resolution
