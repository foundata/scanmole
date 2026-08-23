"""Automatic paper-boundary detection for scans of the full device window.

Policy, not PNM mechanics: :mod:`scanmole.pnm` owns parsing, validation,
atomic replacement and pixel-level primitives, while this module decides
where the paper is and what to keep. The dependency runs one way, so the
raster layer stays usable without any cropping policy.

Content-based fallback sizing and standard paper-size selection are a
separate decision and live in :mod:`scanmole.sizing`; this module only
reports the physical edges it can measure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scanmole.pnm import read_header, replace_file

LOGGER = logging.getLogger(__name__)

PAPER_BRIGHTNESS_CUTOFF = 0.7
"""Column/row mean above which a scan line counts as paper, not backing.

Measured on real hardware: ADF backing scans at roughly 0.35 to 0.55 mean
brightness (gray backing, end-of-paper padding) while paper stays above 0.9,
so the cutoff sits comfortably between the two clusters. Scanners with white
backing produce no edge below the cutoff and the page is simply kept whole.
"""

_MIN_PAPER_PX = 16
"""Reject a detected paper box smaller than this per axis as noise."""


_MIN_COLUMN_PAPER_RUN_MM = 2.0
"""How far a column profile must stay paper-bright to end a side walk.

A lone bright column is not evidence of paper. Two such columns are
measured: a saturated sensor strip 1.61 mm wide at the frame edge, and
ordinary backing noise whose mean touches the cutoff for a single
column. Both used to end the walk immediately and keep the backing.

This is not free. Content whose own bright margin is shorter than this
run can be classified as backing and cropped away; that is the accepted
limitation documented for automatic page size, and a fixed page size
avoids the walk entirely. Applies to the side (column) walk only; the
row walk is unchanged. Measured insensitive from 2 to 8 mm across the
corpus, so this is the smallest stable value rather than a tuned one.
"""

_MIN_BACKING_PROFILE_SHARE = 0.80
"""Share of profile positions that must be below the cutoff to skip a run.

Counted over *profile positions*, never over raster pixels: the walk
only ever sees per-column means. Guards the one decision that can
discard evidence, so a short bright run is only dropped when what lies
between it and the real paper reads as backing.

That reading is a density judgement, not a recognition of content.
Ordinary sparse content and alternating patterns (a barcode at the
paper edge) leave the gap around half bright and keep their outer edge;
backing runs 0.96 and above. Dense edge-adjacent content fills the gap
just as backing does and is indistinguishable here. Measured
insensitive from 0.60 to 0.95 on every primary case.
"""


def _paper_edge(
    size: int,
    is_paper: Callable[[int], bool],
    start: int,
    step: int,
    run: int,
) -> int | None:
    """One side of the column walk: where the paper starts, or ``None``.

    ``None`` means no paper-like position at all, or none that stays
    paper-bright for ``run`` positions; the caller treats that as "no
    plausible paper" exactly as a converged walk used to.

    A short bright run at the very edge is discarded only when the gap
    between it and the first sustained run is predominantly dark, which
    is what backing looks like. The gap deliberately excludes the outer
    run itself: including it would make the verdict depend on how wide
    that run happens to be, which is a property of the sensor rather
    than of the page.

    This validates edge evidence; it cannot recognise content. Sparse
    and alternating content leaves the gap too bright to skip, so it
    survives, but a dense region preceded by less than ``run`` of bright
    paper fills the gap exactly as backing does and is cropped with it.
    """
    first: int | None = None
    sustained: int | None = None
    run_start = start
    count = 0
    index = start
    while 0 <= index < size:
        if is_paper(index):
            if count == 0:
                run_start = index
                if first is None:
                    first = index
            count += 1
            if count >= run:
                sustained = run_start
                break
        else:
            count = 0
        index += step
    if first is None or sustained is None:
        return None
    if sustained == first:
        return first  # today's answer already stays paper-bright
    gap = first
    while 0 <= gap < size and is_paper(gap):
        gap += step  # step over the complete outer run
    if not (0 <= gap < size):  # pragma: no cover -- the run would be sustained
        return first
    positions = range(gap, sustained, step)  # directional [gap, sustained)
    dark = sum(1 for position in positions if not is_paper(position))
    if dark / len(positions) >= _MIN_BACKING_PROFILE_SHARE:
        return sustained
    return first


@dataclass(frozen=True)
class _Bounds:
    """Inclusive pixel bounds of the paper found in one frame.

    What the evidence walks produce and the shared finalization consumes,
    so a second raster format can contribute its own evidence later
    without repeating the trim, minimum-size and no-op rules.
    """

    left: int
    top: int
    right: int
    bottom: int


def _finalize(bounds: _Bounds, width: int, height: int, trim_px: int) -> _Bounds | None:
    """Turn measured bounds into the box to keep, or ``None`` for no crop.

    ``None`` covers a box too small to be plausible paper and a box that
    is the whole frame already. Trimming shaves transition pixels only
    off edges the walk actually detected (moved off the frame boundary):
    an unresolved edge was never measured and may carry content in its
    outermost rows. The result never reaches past the frame, because
    every step here only ever shrinks.
    """
    left, top, right, bottom = bounds.left, bounds.top, bounds.right, bounds.bottom
    if bottom - top < _MIN_PAPER_PX:
        return None
    if (left, top, right, bottom) == (0, 0, width - 1, height - 1):
        return None  # no backing visible anywhere; nothing to crop
    if left > 0:
        left += trim_px
    if right < width - 1:
        right -= trim_px
    if top > 0:
        top += trim_px
    if bottom < height - 1:
        bottom -= trim_px
    if right - left < _MIN_PAPER_PX or bottom - top < _MIN_PAPER_PX:
        return None
    return _Bounds(left, top, right, bottom)


def _gray_bounds(
    raster: bytes,
    *,
    width: int,
    height: int,
    maxval: int,
    channels: int,
    deep: bool,
    dpi: int,
    feeder_band_px: int | None,
) -> _Bounds | None:
    """Where the paper lies in a gray or color raster, or ``None``.

    ``None`` means no plausible paper was found, which keeps the full
    frame: an all-dark frame, a jam, or a full-bleed page. The caller
    turns the bounds into a crop; nothing here touches the file.
    """
    gray = raster
    if deep:  # 16-bit big-endian: the high bytes carry the significant part
        gray = gray[0::2]
    if channels == 3:
        gray = gray[1::3]  # green channel
    cutoff = PAPER_BRIGHTNESS_CUTOFF * (maxval >> 8 if deep else maxval)

    # Column profile over a row subsample (C-speed slices); row profile over a
    # column subsample restricted to the detected paper columns, so the side
    # backing cannot drag content rows below the cutoff.
    paper_run_px = max(1, round(_MIN_COLUMN_PAPER_RUN_MM * dpi / 25.4))

    def column_walk(last_row: int) -> tuple[int, int]:
        row_step = max(1, last_row // 512)
        sampled = b"".join(
            gray[row * width : (row + 1) * width]
            for row in range(0, last_row, row_step)
        )
        sampled_rows = len(sampled) // width
        verdict: dict[int, bool] = {}

        def column_is_paper(column: int) -> bool:
            decided = verdict.get(column)
            if decided is None:
                decided = sum(sampled[column::width]) / sampled_rows >= cutoff
                verdict[column] = decided
            return decided

        found_left = _paper_edge(width, column_is_paper, 0, 1, paper_run_px)
        found_right = _paper_edge(width, column_is_paper, width - 1, -1, paper_run_px)
        if found_left is None or found_right is None:
            # No plausible paper, reported exactly as a converged walk used
            # to be, so the feeder-band fallback below still engages.
            return 0, 0
        return found_left, found_right

    left, right = column_walk(height)
    if right - left < _MIN_PAPER_PX:
        # No plausible paper over the full height. On a feeder frame the
        # paper is top-anchored, so a huge window's synthetic tail can
        # dilute every column mean below the cutoff; re-derive the
        # columns from the leading-edge band, where feeder paper must
        # start. Anything else keeps the full frame.
        if feeder_band_px is None:
            return None
        left, right = column_walk(min(feeder_band_px, height))
        if right - left < _MIN_PAPER_PX:
            return None  # genuinely no paper (all-dark, jam, full bleed)

    column_step = max(1, (right - left + 1) // 512)

    def row_is_paper(row: int) -> bool:
        segment = gray[row * width + left : row * width + right + 1 : column_step]
        return sum(segment) / len(segment) >= cutoff

    top, bottom = 0, height - 1
    # End-of-paper padding can be as bright as paper: some devices pad color
    # and back-side passes with pure white, invisible to the brightness walk.
    # It is also indistinguishable from a genuine paper margin that the
    # scanner white-clipped to full brightness, so no image-only heuristic
    # may strip it (a bit-perfectly uniform run proves nothing: clipping
    # flattens sensor noise too). The axis stays at the scan window and the
    # per-axis content sizing decides its real extent.
    while top < bottom and not row_is_paper(top):
        top += 1
    while bottom > top and not row_is_paper(bottom):
        bottom -= 1
    return _Bounds(left, top, right, bottom)


def autocrop_pnm(
    path: Path, trim_px: int, feeder_band_px: int | None = None, *, dpi: int
) -> bool:
    """Crop a raw gray/color PNM to the detected paper edges, in place.

    Scanning the device's full window (page size ``auto``) surrounds the
    paper with the darker ADF backing and end-of-paper padding. This walks
    the column and row mean-brightness profiles inward from each edge until
    they cross :data:`PAPER_BRIGHTNESS_CUTOFF`, then rewrites the file
    cropped to that box, shaved inward by ``trim_px`` on each side the walk
    actually moved, so the half-gray transition pixels of a detected edge
    cannot survive as a dark rim (which would both look bad after 1-bit
    conversion and rescue blank pages from the blank drop). Printed content
    never sits at a physically detected paper edge, so the shave is safe
    there; an edge the walk left at the frame boundary was never detected,
    may carry content up to its first row, and keeps every row.

    The side walk additionally requires the crossing to hold for
    :data:`_MIN_COLUMN_PAPER_RUN_MM` (hence ``dpi``), because a single
    paper-bright column is not evidence of paper: a saturated sensor
    strip at the frame edge and backing noise touching the cutoff both
    used to end the walk immediately and keep the backing. A short outer
    run is only discarded when the gap to the first sustained run is
    predominantly dark; see :func:`_paper_edge`. The row walk is
    deliberately unchanged, since the measured defects are all side
    edges.

    ``feeder_band_px`` enables the feeder-only fallback for top-anchored
    frames (the caller states that context explicitly; it is never guessed
    from pixels): when a huge scan window fills most of every column with
    synthetic padding (the Brother ADS-4550W simplex window pads with
    mid-gray), the full-height column means all sink below the paper
    cutoff and no plausible paper is found. The fallback re-derives the
    column profile from the leading-edge band alone, where feeder paper
    must start, and then runs the ordinary row walk within those columns.
    The band must stay shorter than the shortest plausible document (a
    short receipt); the pipeline passes about 50 mm.

    Already-1-bit (``P4``) and non-PNM files are left alone: 1-bit padding is
    indistinguishable from the page's own white margin, so native-lineart
    devices rely on hardware lower-edge detection instead (``--ald``, see the
    scan command assembly).

    Returns:
        Whether the file was rewritten. ``False`` also covers "no backing
        visible" (borderless scan or white backing) and "no paper found" (a
        safety fallback keeping the full frame).

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"5", b"6"):
        return False
    kind = buffer[1:2]
    tokens, offset = read_header(buffer, 3)
    try:
        width, height, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")

    channels = 3 if kind == b"6" else 1
    deep = maxval > 255
    pixel_bytes = channels * (2 if deep else 1)
    row_bytes = width * pixel_bytes
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = buffer[offset : offset + row_bytes * height]

    bounds = _gray_bounds(
        raster,
        width=width,
        height=height,
        maxval=maxval,
        channels=channels,
        deep=deep,
        dpi=dpi,
        feeder_band_px=feeder_band_px,
    )
    if bounds is None:
        return False
    kept = _finalize(bounds, width, height, trim_px)
    if kept is None:
        return False

    start = kept.left * pixel_bytes
    stop = (kept.right + 1) * pixel_bytes
    data = b"".join(
        raster[row * row_bytes + start : row * row_bytes + stop]
        for row in range(kept.top, kept.bottom + 1)
    )
    magic = b"P6" if kind == b"6" else b"P5"
    header = b"%s\n%d %d\n%d\n" % (
        magic,
        kept.right - kept.left + 1,
        kept.bottom - kept.top + 1,
        maxval,
    )
    replace_file(path, header + data)
    return True


def autocrop_image(
    path: Path, trim_px: int, feeder_band_px: int | None = None, *, dpi: int
) -> bool:
    """Best-effort in-place crop to the paper edges; never fails the page.

    Returns:
        Whether the file was cropped. A malformed file is left untouched with
        a warning, so the page still reaches the rest of the pipeline.
    """
    try:
        return autocrop_pnm(path, trim_px, feeder_band_px, dpi=dpi)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot crop %s: %s", path, exc)
        return False
