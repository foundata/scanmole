"""Host-side raster deskew: measure with Tesseract, rotate with Pillow.

The default owner of the deskew request, and the one that needs nothing
of the scanner: :mod:`scanmole.deskew_policy` settles ownership from the
capability listing and reports it as ``EffectiveSettings.deskew_applied``,
which normally leaves the job here; a backend takes it only when asked
for explicitly or when its own option cannot be switched off. ocrmypdf's
``--deskew`` stays the fallback for pages this path cannot own. Exactly one of them ever runs, because
resampling a page twice costs more than the skew it removes.

Available everywhere is not the same as effective everywhere. A page
with too little text to analyse yields no angle, a skew under
:data:`MIN_ANGLE_DEGREES` is not worth the resampling, and a deep or
oversized raster is refused outright. Each of those leaves the page as
it was, which is the point: a measurement nobody can trust is not a
reason to turn the paper.

Measuring and rotating are split on purpose. Tesseract already ships as
an install requirement and reports a page's skew from its own layout
analysis, which is the same evidence ocrmypdf uses and needs no new
tool. Pillow does the rotation, because doing it well means resampling
and the standard library has no image support at all.

Three outcomes carry the whole contract. ``STRAIGHT`` means the page was
measured and needs nothing, valid no-evidence results included: it is
final, and nobody downstream should rotate it again. ``DECLINED`` means
the page was measured but turning it inside its own canvas would push
meaningful content off the page; it keeps its skew, finally, and nobody
downstream may attempt the same unsafe turn. ``UNSUPPORTED`` means this
path cannot own the page at all, so the request falls through
to whatever comes next. Failures are neither: a timeout, an interrupt or
any write problem propagates as what it is, because the pipeline, not
this module, knows what a run owes the paper. It translates a broken
attempt into the documented processing failure and keeps the pages the
feeder already swallowed.
"""

from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from scanmole.external import run_command
from scanmole.pnm import coherent_regions, pnm_evidence_mask, read_header, replace_file

LOGGER = logging.getLogger(__name__)

ANGLE_TOOL = "tesseract"
"""The measurement tool, already required for OCR and named as it installs."""

TIMEOUT_SECONDS = 120.0
"""How long one page may take to measure.

Its own budget, not the hour the PDF and OCR stages get: those run once
over a whole document, while this runs per page and was measured at 0.24
to 0.44 seconds for A4 at 300 dpi across 1-bit, gray and color. Two
minutes leaves that more than two orders of magnitude of headroom while
still bounding a wedged tool to something a person will wait through.
"""

MIN_ANGLE_DEGREES = 0.1
"""Below this the page counts as straight and is left untouched.

Rotating by less than a tenth of a degree moves a 300 dpi A4 page's
corner by under two pixels while resampling every one of them, so it
costs sharpness and buys nothing. It is also inside what the measurement
itself can resolve: over controlled rotations the reported angle tracked
the real one to within 0.03 degrees, never closer.
"""

MAX_PIXELS = 80_000_000
"""Frames larger than this are refused before either tool sees them.

Our own bound rather than Pillow's: it sits above every frame the
corpus holds (27 megapixels for a multi-metre feeder window) and above
A4 at 600 dpi (35), and below Pillow's default decompression-bomb guard
(89.5), so an oversized unresolved window is refused here, predictably
and with the guard still armed behind it.
"""

_TOOL_FAILED = 2
"""Exit codes at or above this are the tool failing, not an empty page.

Measured: Tesseract exits 0 with its layout analysis, 1 on a page too
sparse to analyse (no output at all), and 2 when it could not read the
input. Only the last is a reason to stop the run.
"""

_MODES = {
    b"4": ("1", Image.Resampling.NEAREST),
    b"5": ("L", Image.Resampling.BICUBIC),
    b"6": ("RGB", Image.Resampling.BICUBIC),
}
"""Raster families this path handles, with the resampling each gets.

A 1-bit page has no shades to interpolate between, so anything but
nearest neighbour would invent gray it cannot store; gray and color get
bicubic, which is what ocrmypdf uses for the same job.
"""

_FILL: dict[bytes, float | tuple[float, ...]] = {
    b"4": 1,
    b"5": 255,
    b"6": (255, 255, 255),
}
"""White in each accepted mode, for the corner the rotation exposes."""

_MEANINGFUL_MAX_SPAN = 0.9
"""A coherent region spanning at least this fraction of a frame axis,
while hugging a frame edge, is a scanner artifact rather than content.
Boundary bars run the full height and trailing-edge shadows the full
width of the frame; no printed content does."""

_MEANINGFUL_MAX_ASPECT = 12.0
"""An edge-hugging region longer than this many times its own thickness
is an artifact hairline. Interior rules and underlines are exempt on
purpose: a line three millimetres inside the paper is decoration worth
protecting, one on the frame boundary is the scanner's."""

_EDGE_MARGIN_MM = 1.5
"""How close to the frame border a region must reach before the span
and aspect artifact rules apply to it at all."""

_SAFETY_MARGIN_PX = 2
"""Resampling reads neighbours, so content must keep this distance from
the canvas boundary after the turn to survive it whole."""


class Deskewed(Enum):
    """What happened to one page."""

    ROTATED = "rotated"
    STRAIGHT = "straight"
    DECLINED = "declined"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DeskewResult:
    """The outcome of one deskew attempt, and the angle if it turned."""

    outcome: Deskewed
    degrees: float = 0.0


@dataclass(frozen=True)
class _Raster:
    """A frame this path is willing to work on."""

    kind: bytes
    width: int
    height: int


def _inspect(buffer: bytes) -> _Raster | None:
    """The frame's geometry, or ``None`` when the host cannot own it.

    Everything refused here is refused before either tool runs: an
    unreadable header, a nonsensical size, a depth Pillow would quietly
    reduce, and a frame large enough to be worth refusing on its own
    terms rather than discovering halfway through a rotation.
    """
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in _MODES:
        return None
    kind = buffer[1:2]
    try:
        tokens, _offset = read_header(buffer, 2 if kind == b"4" else 3)
        width, height = int(tokens[0]), int(tokens[1])
        maxval = 1 if kind == b"4" else int(tokens[2])
    except (ValueError, IndexError):
        return None
    if width <= 0 or height <= 0 or maxval <= 0:
        return None
    if maxval > 255:
        # Pillow reads a 16-bit P6 as 8-bit and would write it back that
        # way, and a 16-bit P5 opens in a mode whose white is 65535, so
        # an ordinary white fill would come out nearly black.
        return None
    if width * height > MAX_PIXELS:
        return None
    return _Raster(kind, width, height)


def page_angle(path: Path) -> float | None:
    """Tesseract's skew estimate for ``path`` in degrees, or ``None``.

    The exit code decides first and the output is read only after it
    allows one, because a run that failed can still have printed an
    angle-shaped line on its way there. Exit 0 is a measurement, exit 1
    is a page too sparse to analyse and carries no angle whatever it
    left on stderr, and anything above is the tool failing.

    ``None`` therefore means no usable evidence: a declined page, no
    angle line, or a line that is not a finite number. Every one of
    those is a reason to leave the page alone rather than to guess. The
    exit code is what says which, never the tool's prose, which is
    localized and not a contract.

    The sign is Tesseract's own and needs no flipping: measured over
    controlled rotations, passing the converted degrees straight to
    Pillow undoes the skew, which is also what ocrmypdf does with the
    same number.

    Raises:
        subprocess.CalledProcessError: If the tool could not read the
            page at all.
        subprocess.TimeoutExpired: If it did not finish in time.
    """
    command = [ANGLE_TOOL, "--psm", "2", str(path), "-"]
    result = run_command(command, timeout_seconds=TIMEOUT_SECONDS)
    if result.returncode >= _TOOL_FAILED:
        raise subprocess.CalledProcessError(
            result.returncode, command, result.stdout, result.stderr
        )
    if result.returncode != 0:
        LOGGER.debug("%s: declined by the measurement, no angle", path.name)
        return None
    # The angle goes to stderr beside the layout analysis.
    for line in result.stderr.splitlines():
        _, separator, value = line.partition("Deskew angle:")
        if not separator:
            continue
        try:
            radians = float(value.strip())
        except ValueError:
            LOGGER.debug("%s: unreadable deskew angle %r", path.name, value.strip())
            return None
        if not math.isfinite(radians):
            LOGGER.debug("%s: non-finite deskew angle %r", path.name, value.strip())
            return None
        return math.degrees(radians)
    return None


def _meaningful_box(
    buffer: bytes, raster: _Raster, dpi: int
) -> tuple[int, int, int, int] | None:
    """The union box of the frame's meaningful content, or ``None``.

    Meaningful is what the turn must not lose: every locally coherent
    ink region except the scanner's own edge artifacts. A region that
    reaches the frame border and either spans an axis (a boundary bar,
    a trailing-edge shadow) or is a hairline is the scanner's, however
    dark; a corner mark, an inset rule and every interior region are
    content. ``None`` means no coherent evidence at all: nothing is
    established that a turn could lose, so nothing constrains it.
    """
    try:
        mask = pnm_evidence_mask(buffer, 0.5)
        if mask is None:  # pragma: no cover -- _inspect admits P4/P5/P6 only
            return None
        regions = coherent_regions(mask, dpi)
    except ValueError:  # pragma: no cover -- _inspect already parsed it
        return None
    edge_px = max(1, round(_EDGE_MARGIN_MM * dpi / 25.4))
    meaningful: list[tuple[int, int, int, int]] = []
    for region in regions:
        x0, y0, x1, y1 = region.box
        touches = (
            x0 <= edge_px
            or y0 <= edge_px
            or x1 >= raster.width - edge_px
            or y1 >= raster.height - edge_px
        )
        if touches:
            width, height = x1 - x0, y1 - y0
            spans = (
                width >= raster.width * _MEANINGFUL_MAX_SPAN
                or height >= raster.height * _MEANINGFUL_MAX_SPAN
            )
            hairline = max(width, height) > min(width, height) * _MEANINGFUL_MAX_ASPECT
            if spans or hairline:
                continue
        meaningful.append(region.box)
    if not meaningful:
        return None
    return (
        min(box[0] for box in meaningful),
        min(box[1] for box in meaningful),
        max(box[2] for box in meaningful),
        max(box[3] for box in meaningful),
    )


def _turn_plan(
    box: tuple[int, int, int, int], raster: _Raster, degrees: float
) -> tuple[int, int] | None:
    """How to keep ``box`` on the canvas through the turn, if possible.

    Pillow rotates counterclockwise around the canvas centre; a source
    point lands at ``c + R(p - c)``. Returns the post-rotation shift
    that keeps every corner of ``box`` inside the canvas: ``(0, 0)``
    when the plain turn is already safe (that path stays byte for byte
    the one it always was), a small translation when the turned box
    fits the canvas but sticks out on one side, and ``None`` when it
    cannot fit without scaling, which is never an option.
    """
    theta = math.radians(degrees)
    cos, sin = math.cos(theta), math.sin(theta)
    cx, cy = raster.width / 2, raster.height / 2
    corners = (
        (box[0], box[1]),
        (box[2] - 1, box[1]),
        (box[0], box[3] - 1),
        (box[2] - 1, box[3] - 1),
    )
    xs = []
    ys = []
    for x, y in corners:
        dx, dy = x - cx, y - cy
        xs.append(cx + dx * cos + dy * sin)
        ys.append(cy - dx * sin + dy * cos)
    margin = _SAFETY_MARGIN_PX
    low_x, high_x = margin, raster.width - 1 - margin
    low_y, high_y = margin, raster.height - 1 - margin
    if (
        min(xs) >= low_x
        and max(xs) <= high_x
        and min(ys) >= low_y
        and max(ys) <= high_y
    ):
        return (0, 0)
    if max(xs) - min(xs) > high_x - low_x or max(ys) - min(ys) > high_y - low_y:
        return None
    shift_x = 0
    if min(xs) < low_x:
        shift_x = math.ceil(low_x - min(xs))
    elif max(xs) > high_x:
        shift_x = -math.ceil(max(xs) - high_x)
    shift_y = 0
    if min(ys) < low_y:
        shift_y = math.ceil(low_y - min(ys))
    elif max(ys) > high_y:
        shift_y = -math.ceil(max(ys) - high_y)
    return (shift_x, shift_y)


def _rotate(
    path: Path, raster: _Raster, degrees: float, shift: tuple[int, int]
) -> bool:
    """Rotate a supported PNM in place; ``False`` means it was refused.

    Raises:
        OSError: If staging or replacing the file fails. The original
            frame stays intact and no staging file survives, but the
            caller has to hear about it: the page may be the only copy
            of the paper.
    """
    mode, resample = _MODES[raster.kind]
    try:
        with Image.open(path) as image:
            if image.mode != mode or image.size != (raster.width, raster.height):
                LOGGER.info(
                    "%s: unexpected raster %s %s; keeping it",
                    path.name,
                    image.mode,
                    image.size,
                )
                return False
            if shift == (0, 0):
                # The safe path is the one it always was, byte for byte.
                turned = image.rotate(
                    degrees, resample=resample, fillcolor=_FILL[raster.kind]
                )
            else:
                turned = image.rotate(
                    degrees,
                    resample=resample,
                    fillcolor=_FILL[raster.kind],
                    translate=shift,
                )
    except Image.DecompressionBombError:  # pragma: no cover -- MAX_PIXELS is lower
        LOGGER.warning("%s: too large to rotate safely; keeping it", path.name)
        return False
    except (UnidentifiedImageError, ValueError) as exc:
        LOGGER.warning("cannot rotate %s: %s", path.name, exc)
        return False
    staging = path.with_name(f".{path.name}.rot")
    try:
        turned.save(staging, format="PPM")
        replace_file(path, staging.read_bytes())
    finally:
        staging.unlink(missing_ok=True)
    return True


def deskew_page(path: Path, dpi: int = 300) -> DeskewResult:
    """Straighten one acquired page without losing its detected content.

    Measures the skew, checks which meaningful pixels the turn would
    push off the canvas, and rotates the raster inside its own canvas
    with a white fill, shifted just enough to keep every meaningful
    region aboard where the plain turn would clip one. The dimensions
    never change and the page is never scaled; a turn that cannot keep
    the content whole is declined and the page keeps its skew. ``dpi``
    scales the content-evidence tiles; the caller's best knowledge is
    enough, and 300 is the assumption where there is none.

    Returns:
        ``ROTATED`` with the applied angle; ``STRAIGHT`` when the page
        was measured and needs nothing, a valid no-evidence result
        included; ``DECLINED`` when only an unsafe turn could straighten
        it, so it stays as scanned and final; ``UNSUPPORTED`` when this
        path cannot own the page, so the caller's own fallback still
        holds the request.

    Raises:
        OSError: If the page cannot be read, staged or replaced.
        subprocess.SubprocessError: If the measurement fails or times
            out. Both leave the acquired raster untouched and reach the
            pipeline, which preserves the pages it already has.
    """
    buffer = path.read_bytes()
    raster = _inspect(buffer)
    if raster is None:
        return DeskewResult(Deskewed.UNSUPPORTED)
    degrees = page_angle(path)
    if degrees is None or abs(degrees) < MIN_ANGLE_DEGREES:
        return DeskewResult(Deskewed.STRAIGHT)
    shift = (0, 0)
    box = _meaningful_box(buffer, raster, dpi)
    if box is not None:
        plan = _turn_plan(box, raster, degrees)
        if plan is None:
            LOGGER.warning(
                "%s: straightening by %.2f degrees would push content off "
                "the page; keeping it as scanned",
                path.name,
                degrees,
            )
            return DeskewResult(Deskewed.DECLINED, degrees)
        shift = plan
    if not _rotate(path, raster, degrees, shift):
        return DeskewResult(Deskewed.UNSUPPORTED)
    if shift == (0, 0):
        LOGGER.info("%s: deskewed by %.2f degrees", path.name, degrees)
    else:
        LOGGER.info(
            "%s: deskewed by %.2f degrees, shifted by (%d, %d) px to keep "
            "the content aboard",
            path.name,
            degrees,
            shift[0],
            shift[1],
        )
    return DeskewResult(Deskewed.ROTATED, degrees)
