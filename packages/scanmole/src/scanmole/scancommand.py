"""Assemble the ``scanimage`` command line for one negotiated scan.

The command and the :class:`EffectiveSettings` it will run under are decided
here, from a capability snapshot alone: no process is spawned and nothing is
read from the device. Acquisition lives in :mod:`scanmole.scanner`, which
drives the command this module produces.

Emission and state are kept apart throughout. A read-only option must not be
written, yet the device still sits on a known value, and the pipeline sizes,
pairs and straightens pages by that value rather than by what was requested.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from scanmole.config import ScanConfig
from scanmole.deskew_policy import plan_deskew
from scanmole.devices import backend_name
from scanmole.errors import DeviceError
from scanmole.negotiation import (
    Plan,
    Support,
    negotiate,
    require_supported,
)
from scanmole.options import (
    Capability,
    format_mm,
    is_flatbed_source,
    parse_page_size,
    readable_capability,
    writable_capability,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveSettings:
    """The backend state the scan will actually run in.

    ``source`` and ``mode`` are the values the device will really be on,
    which is not the same as the values the command emits: a read-only
    option is never written yet still reports what it is set to, and that
    is what lands here. ``None`` therefore means no backend value could
    be established, never merely that nothing was emitted; a request the
    capabilities could not confirm stays ``None`` rather than passing
    itself off as evidence. ``resolution`` is the dpi the scan will use
    after capability snapping, which may differ from the requested one.
    """

    source: str | None
    mode: str | None
    resolution: int | None
    window_mm: tuple[float, float] | None = None
    """The ``-x``/``-y`` scan window the acquisition will use, if known.

    Either the clamped values the command requested, or the current ones
    a read-only axis reports. Lets the pipeline recognize frames that came
    back at the full window: the proof that no hardware paper-length
    detection took place.
    """
    deskew_applied: bool = False
    """Whether a backend deskew option took the deskew request.

    Decides the next step of the deskew cascade: without a backend option
    the pipeline hands the job to OCR, and failing that warns, so the
    request is never a silent no-op.
    """
    faint_native: bool = False
    """Whether a native text enhancement serves the faint request.

    On this path 1-bit frames are the enhanced result the user asked for;
    on every other ``lineart-auto`` path an arriving 1-bit frame proves
    the faint request cannot be satisfied and the pipeline must stop.
    """
    duplex: bool = False
    """Whether the negotiated source delivers one sheet as two frames.

    The single place that decides duplex pairing. A collect run counts
    physical sheets with it and the pipeline pairs front and back frames
    into one paper size with it, so a reported sheet count can never
    contradict the sizing decision.
    """


def _window_cap(page: Capability | None, axis: Capability | None) -> Capability | None:
    """Pick the capability that carries the device's true window limit.

    Prefers the page geometry capability when it is a range with a known
    maximum, falling back to the axis (``-x``/``-y``) capability otherwise.
    """
    if page is not None and page.kind == "range" and page.maximum is not None:
        return page
    return axis


def _single_sheet_count(plan: Plan) -> int:
    """The frame limit that represents one physical sheet.

    Keyed on the conclusively negotiated effective source: a duplex source
    delivers a sheet as two frames, every other source as one. A degraded
    source counts by what the scanner will actually deliver, not by the
    request.

    Raises:
        DeviceError: If the source evidence is UNKNOWN. Without it ScanMole
            cannot promise one physical sheet, so it refuses before feeding
            paper.
    """
    if not plan.source.conclusive:
        raise DeviceError(
            "cannot scan a single sheet: the paper source could not be "
            "negotiated conclusively, so one sheet may be one frame or two "
            "(scan with --sheet-flow stack instead)"
        )
    return 2 if plan.source.effective == "adf-duplex" else 1


_WINDOW_VALUE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)?")


def _observed_window_mm(caps: dict[str, Capability], option: str) -> float | None:
    """The scan extent a read-only ``-x``/``-y`` reports it is fixed at.

    A ``[read-only]`` window option cannot be set, but its current value
    still states the width or height the backend will actually use, which
    is what automatic page size compares each frame against. Only a plain,
    finite, positive number counts: a range maximum is a limit rather than
    the window in force, and anything else is not evidence.
    """
    capability = readable_capability(caps, option)
    if capability is None or capability.settable:
        return None
    match = _WINDOW_VALUE.fullmatch((capability.current or "").strip())
    if match is None:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) and value > 0 else None


def build_scan_command(
    config: ScanConfig,
    device: str,
    caps: dict[str, Capability],
    batch_pattern: str,
    plan: Plan | None = None,
    batch_start: int | None = None,
) -> tuple[list[str], EffectiveSettings]:
    """Assemble the ``scanimage`` command for a batch scan.

    Only options the device actively advertises (per ``caps``) are included.
    The source, mode and resolution come from the negotiated ``plan`` (one
    is computed from ``caps`` when the caller has none), so fallback policy
    lives in one place. ``batch_start`` numbers a collect continuation
    segment (``--batch-start=N``); the first invocation of every flow omits
    it.

    Raises:
        DeviceError: If the plan marks the source or mode UNSUPPORTED, or a
            single-sheet flow lacks conclusive source evidence.

    Returns:
        The command and the settings the scan will actually run with.
    """
    if plan is None:
        plan = negotiate(
            caps,
            source=config.source,
            mode=config.mode,
            resolution=config.resolution,
            lineart_threshold=config.lineart_threshold,
        )
    require_supported(plan)
    command = ["scanimage", "-d", device]

    # Emission and state are two different questions: a read-only option
    # must not be written, yet the device is still on a known value, and
    # the pipeline sizes pages by it.
    if plan.source.backend_value is not None:
        command += ["--source", plan.source.backend_value]
    if plan.mode.backend_value is not None:
        command += ["--mode", plan.mode.backend_value]
    source = plan.source.actual
    mode = plan.mode.actual
    # A native faint-text enhancement's ordered settings follow the mode
    # they were verified against; the adaptive faint path pins the 8-bit
    # depth the guarded threshold needs.
    for extra_option, extra_value in plan.extra_options:
        command += [extra_option, extra_value]
    if plan.depth.backend_value is not None:
        command += ["--depth", plan.depth.backend_value]
    if plan.resolution.backend_value is not None:
        command += ["--resolution", plan.resolution.backend_value]
    # The settings carry the *established* dpi (empty for UNKNOWN): a
    # fixed backend contributes it without any --resolution being emitted,
    # and the requested dpi never masquerades as an established one.
    resolution = (
        int(plan.resolution.effective) if plan.resolution.effective.isdigit() else None
    )

    size = parse_page_size(config.page_size)
    if size is None:
        # Auto page size: scan the device's full window (it clamps oversized
        # requests itself); the pipeline crops each page to the paper edges.
        width, height = float("inf"), float("inf")
    else:
        width, height = size
    # Some backends cap the advertised -x/-y ranges at the current window
    # (fujitsu reports A4 height until --page-height is raised), so the scan
    # area is clamped against the page geometry maxima where the backend has
    # them; --page-width/--page-height are emitted first to extend the window.
    width_cap = _window_cap(
        writable_capability(caps, "page-width"), writable_capability(caps, "x")
    )
    height_cap = _window_cap(
        writable_capability(caps, "page-height"), writable_capability(caps, "y")
    )
    has_x = writable_capability(caps, "x") is not None
    has_y = writable_capability(caps, "y") is not None
    window: dict[str, float] = {}
    for option, value, capability in (
        ("--page-width", width, writable_capability(caps, "page-width")),
        ("--page-height", height, writable_capability(caps, "page-height")),
        ("-x", width, width_cap if has_x else None),
        ("-y", height, height_cap if has_y else None),
    ):
        if capability is None:
            continue
        if value == float("inf") and (
            capability.kind != "range" or capability.maximum is None
        ):
            continue  # no known maximum: let the backend's default window apply
        rendered = format_mm(value, capability, option)
        command += [option, rendered]
        if option in ("-x", "-y"):
            window[option] = float(rendered)
    for option, name in (("-x", "x"), ("-y", "y")):
        # A read-only axis was skipped above, because nothing may be
        # emitted for it. Its current value is still the window the scan
        # will run in, and automatic page size needs that comparison.
        if option not in window:
            observed = _observed_window_mm(caps, name)
            if observed is not None:
                window[option] = observed
    if size is None and writable_capability(caps, "ald") is not None:
        # Auto page size: let the scanner detect the paper's lower edge, so
        # frames come back at true paper length instead of the padded window.
        # Essential for native lineart, where the padding below the paper is
        # bit-identical to the page's own white margin and software cropping
        # cannot tell them apart (verified on the ScanSnap iX100: 297 mm
        # instead of an 895 mm frame).
        command.append("--ald=yes")
    if size is None and writable_capability(caps, "adf-crp") is not None:
        # Same idea on the epsonds backend ("ADF auto cropping"): the device
        # crops to the detected paper bounds itself. White-backing scanners
        # (Epson DS series) need this, because software edge detection cannot
        # tell white backing from white paper.
        command.append("--adf-crp=yes")

    if config.despeckle > 0 and writable_capability(caps, "swdespeck") is not None:
        command.append(f"--swdespeck={config.despeckle}")
    # Who straightens the page is settled here, from the capabilities
    # alone, and refuses while the stack is still in the feeder where the
    # requested owner cannot be established. Every mechanism is always
    # emitted, including the ones nobody chose, so a device default can
    # never straighten a page the run already accounted for.
    deskew = plan_deskew(
        caps,
        requested=config.deskew,
        method=config.deskew_method,
        backend=backend_name(device),
    )
    command += [f"--{name}={value}" for name, value in deskew.options]
    if deskew.notice is not None and batch_start is None:
        # Once per run: collect rebuilds this command per segment.
        LOGGER.warning("%s", deskew.notice)
    if writable_capability(caps, "swcrop") is not None:
        command.append(f"--swcrop={'yes' if config.crop else 'no'}")

    command += ["--format=pnm", f"--batch={batch_pattern}", "--batch-print"]
    if batch_start is not None:
        command.append(f"--batch-start={batch_start}")
    # Keyed on the *mapped* source: a feeder request degraded to the flatbed
    # (flatbed-only device) must not batch-scan "infinity pages" on hardware
    # that never reports "feeder empty".
    flatbed = plan.source.effective == "flatbed" or (
        source is not None and is_flatbed_source(source)
    )
    # A flatbed never reports "feeder empty", so it always carries a frame
    # limit; a single-sheet flow limits every source to one physical sheet.
    batch_count = 1 if flatbed else None
    if not plan.source.conclusive:
        # The listing proved nothing about the source, so "drain the
        # feeder" is an assumption, not a fact. On a flatbed the scan
        # would never end by itself, so an unproven source gets the
        # flatbed's limit: one frame per invocation. A collect run simply
        # asks again for the next one.
        batch_count = 1
        if batch_start is None:
            # Once per run: collect rebuilds this command per segment.
            LOGGER.warning(
                "the scanner's source capabilities could not be read, so "
                "ScanMole cannot tell a feeder from a flatbed; limiting this "
                "invocation to one frame instead of an open-ended batch"
            )
    if config.sheet_flow == "single":
        batch_count = _single_sheet_count(plan)
    if batch_count is not None:
        command.append(f"--batch-count={batch_count}")
    return command, EffectiveSettings(
        source=source,
        mode=mode,
        resolution=resolution,
        window_mm=(window["-x"], window["-y"]) if len(window) == 2 else None,
        deskew_applied=deskew.applied,
        faint_native=(
            plan.mode.requested == "lineart-auto"
            and plan.mode.support is Support.NATIVE
        ),
        # Pairing needs proof, not a request: an UNKNOWN source echoes the
        # requested value back, and pairing on that would fuse unrelated
        # simplex pages into one sheet for sizing and undercount a
        # collection.
        duplex=plan.source.conclusive and plan.source.effective == "adf-duplex",
    )
