"""Automatic paper-boundary detection for scans of the full device window.

Policy, not PNM mechanics: :mod:`scanmole.pnm` owns parsing, validation,
atomic replacement and pixel-level primitives, while this module decides
where the paper is and what to keep. The dependency runs one way, so the
raster layer stays usable without any cropping policy.

Two kinds of evidence feed one crop, chosen by the raster the device
actually delivered rather than by the mode that was requested. Gray and
color frames (``P5``/``P6``) carry brightness, so the paper edge is where
the profile crosses :data:`PAPER_BRIGHTNESS_CUTOFF`. Native 1-bit frames
(``P4``) have already been thresholded by the scanner and carry ink
density instead, so a paper edge shows up as a boundary dark across most
of the perpendicular axis; see :func:`_lineart_bounds`. Both produce
:class:`_Bounds` and share :func:`_finalize`.

Content-based fallback sizing and standard paper-size selection are a
separate decision and live in :mod:`scanmole.sizing`; this module only
reports the physical edges it can measure.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scanmole.pnm import POPCOUNT, crop_bit_rows, read_header, replace_file

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


_BIT_PLANES = [
    bytes(1 if (value >> (7 - bit)) & 1 else 0 for value in range(256))
    for bit in range(8)
]
"""Translate tables isolating one bit of every ``P4`` raster byte.

Eight tables plus eight strided sums give exact per-column ink counts at
C speed; a per-pixel loop over an A4/300 dpi frame is nine million
iterations and is not an option.
"""

_LINEART_SEARCH_MM = 12.0
"""How far in from a frame edge a 1-bit paper boundary may be found.

The measured boundaries sit within 6.1 mm of their frame edge and the
outer white strip ahead of one runs to 3.2 mm, so the search has to
reach past both. It must not reach much further: print is fair game
beyond it, and at 20 mm five corpus frames stop at their heading
instead of their top border, moving that edge from row 7 to row 216.
Measured insensitive from 12 to 15 mm.
"""

_LINEART_BAND_MM = 8.0
"""Height of one evidence band along the perpendicular axis.

A boundary skewed by a fraction of a degree crosses several positions
over a full page, which flattens its whole-axis profile below any
threshold worth having: measured over the corpus, whole-axis evidence
alone loses 16 borders on 9 of the 40 frames that have one. Within a
band this short the same boundary is effectively straight. Bands may
not get much shorter either, or a single printed mark fills one and
votes; below 6 mm the corpus gains crops of over 100 px driven by
ordinary print. Measured insensitive from 6 to 12 mm.
"""

_LINEART_INK_SHARE = 0.80
"""Ink share within a band that makes a position boundary-dark.

Ink occupancy, not brightness: a ``P4`` position is a run of black and
white bits, so this counts set bits and has nothing to do with
:data:`PAPER_BRIGHTNESS_CUTOFF`. The value is what "spans a large
majority of the perpendicular axis" means in practice. Measured
insensitive from 0.70 to 0.85; at 0.60 the corpus starts reading
halftoned backing texture as a boundary and crops 110 px more.
"""

_LINEART_PAPER_INK_SHARE = 0.05
"""Whole-axis ink share at or below which a position reads as paper.

Deliberately not zero: scanner noise, despeckle residue and the outer
rows of ordinary text all leave a little ink in a paper position, and
the measured margins run to 0.01. Measured insensitive from 0.02 to
0.20; at 0.01 the corpus loses detected edges to their own paper noise.
"""

_LINEART_PAPER_RUN_MM = 2.0
"""How far paper-like evidence must hold just inside the boundary.

A boundary with nothing but more ink behind it is print, not a paper
edge. Measured insensitive from 0.5 to 6 mm on the corpus, so this is
a plausibility floor rather than a tuned length.
"""

_LINEART_BAND_VOTE_SHARE = 0.80
"""Share of bands one boundary must span before a side resolves.

Counted over the bands of a single boundary path, never over bands that
merely found darkness somewhere, which is what stops an isolated mark,
a punch hole or a short rule from becoming an edge; it also leaves room
for the small gaps a real boundary has. Measured insensitive from 0.60
to 0.85; at 0.95 the corpus loses 30 real borders to their own gaps.
"""

_LINEART_TRACK_SKEW = 0.125
"""How far a boundary may move sideways from one band to the next.

Dimensionless, as a fraction of the band height, so it is a skew
tolerance and holds at every resolution: one in eight is about seven
degrees, far past anything a feeder produces (the steepest boundary in
the corpus runs 17 px over 3300 rows, about 0.3 degrees, or one in
190). The generosity is deliberate, because the allowance is not there
to measure skew but to say how far a boundary may travel between one
band and the next. It also draws the line the guarantee rests on: a
mark whose own position is further than this from the boundary in the
neighbouring bands cannot be part of it, while one nearer than this is
inside the boundary's own uncertainty and is cropped with it. The two
measured over-crops had their marks 45 and 10 positions clear of the
boundary. The corpus is unchanged and both stay out from 0.03 to 0.20;
below 0.03 a real boundary starts breaking up, and at 0.21 the nearer
mark comes within reach.
"""

_LINEART_TRACK_GAP = 2
"""Consecutive bands a boundary may skip and still be one track.

About 16 mm at the band height, which covers a punch hole, a despeckled
patch or a torn corner. Only bands that saw nothing at all are bridged:
a band holding a dark interval has answered, and a path that cannot
reach any of its positions ends there rather than around it, which is
what stops a bridge from buying extra sideways room or letting an
unrelated mark stand in for a missing boundary. The share above
independently limits how many bands may be missing in total, so this
only bounds how they may cluster. The corpus is unchanged from 0 to 10
bands, so this is a plausibility bound rather than a tuned length.
"""


def _band_bounds(total: int, size: int) -> list[tuple[int, int]]:
    """Split ``total`` units into contiguous bands of about ``size`` units."""
    count = max(1, total // size)
    step = total // count
    return [
        (index * step, total if index == count - 1 else (index + 1) * step)
        for index in range(count)
    ]


def _mask_padding(strip: bytes, stride: int, mask: int) -> bytes:
    """Clear ``P4`` row-padding bits in the last byte column of a strip.

    The format declares those bits don't-care and producers do leave
    garbage there; counted as ink they would fake a boundary at the right
    frame edge of any frame whose width is not a multiple of eight.
    """
    if mask == 0xFF:
        return strip
    data = bytearray(strip)
    data[stride - 1 :: stride] = bytes(
        byte & mask for byte in data[stride - 1 :: stride]
    )
    return bytes(data)


def _column_ink(
    raster: bytes,
    *,
    row_bytes: int,
    height: int,
    first: int,
    last: int,
    bands: list[tuple[int, int]],
    mask: int,
) -> list[list[int]]:
    """Per-band, per-column ink counts for byte columns ``[first, last)``."""
    stride = last - first
    strip = b"".join(
        raster[row * row_bytes + first : row * row_bytes + last]
        for row in range(height)
    )
    if last == row_bytes:
        strip = _mask_padding(strip, stride, mask)
    counts = [[0] * (stride * 8) for _ in bands]
    for bit, table in enumerate(_BIT_PLANES):
        marks = strip.translate(table)
        for index, (start, stop) in enumerate(bands):
            segment = marks[start * stride : stop * stride]
            for byte in range(stride):
                counts[index][byte * 8 + bit] = sum(segment[byte::stride])
    return counts


def _row_ink(
    raster: bytes,
    *,
    row_bytes: int,
    first: int,
    last: int,
    bands: list[tuple[int, int]],
    mask: int,
) -> list[list[int]]:
    """Per-band, per-row ink counts for rows ``[first, last)``."""
    strip = _mask_padding(
        raster[first * row_bytes : last * row_bytes], row_bytes, mask
    ).translate(POPCOUNT)
    return [
        [
            sum(strip[row * row_bytes + start : row * row_bytes + stop])
            for row in range(last - first)
        ]
        for start, stop in bands
    ]


def _band_edges(band: list[float], window: int) -> list[int]:
    """Innermost position of every dark interval in one band, ascending.

    Intervals separated by so much as one paper-bright position stay
    separate. Their distance may well be inside the geometric tolerance
    the track below works to, but that tolerance is about how far a
    boundary may travel between bands, not about what counts as one
    feature within a band; merging on it would let a mark beside the
    boundary pass for part of it.
    """
    reach = min(window, len(band))
    return [
        index
        for index in range(reach)
        if band[index] >= _LINEART_INK_SHARE
        and (index + 1 >= reach or band[index + 1] < _LINEART_INK_SHARE)
    ]


def _extend(
    behind: list[int], reached: list[int], here: list[int], allowance: int
) -> list[int]:
    """Longest chain ending at each of ``here``, extended from ``behind``.

    Both position lists are ascending, so the admissible predecessors of
    a position form a window that only ever moves forward; a monotonic
    deque keeps the best chain in it, which holds the whole pass linear
    in the number of positions rather than quadratic.
    """
    best = [1] * len(here)
    if not behind:
        return best
    low = high = 0
    window: deque[int] = deque()  # indices into behind, chain lengths descending
    for index, position in enumerate(here):
        while high < len(behind) and behind[high] <= position + allowance:
            while window and reached[window[-1]] <= reached[high]:
                window.pop()
            window.append(high)
            high += 1
        while low < high and behind[low] < position - allowance:
            if window and window[0] == low:
                window.popleft()
            low += 1
        if window:
            best[index] = reached[window[0]] + 1
    return best


def _boundary_track(bands: list[list[float]], *, window: int, slack: int) -> int | None:
    """Innermost position of a boundary the bands agree on, or ``None``.

    Bands voting separately is not enough evidence: each one finding
    darkness *somewhere* lets a single interior mark, deeper than the
    real boundary and unrelated to it, decide the crop. Neither is
    joining that darkness into connected groups, because a group is not
    a boundary: a mark beside the boundary touches it, inherits the
    support the whole edge earned and pulls the crop in behind itself.

    So a boundary is a **path** here, holding at most one position per
    band and stepping no more than ``slack`` per band between them. A
    position counts only when some path through it spans
    :data:`_LINEART_BAND_VOTE_SHARE` of the bands, which two passes
    decide: the longest path reaching it from outside, plus the longest
    leaving it inward, minus itself. A mark the neighbouring bands
    cannot reach at its own position lies on no path but its own, spans
    one band and is ignored however deep it sits.

    A path may bridge up to :data:`_LINEART_TRACK_GAP` bands that hold
    no dark interval at all, with the step allowance growing in
    proportion so a punch hole does not penalise a skewed boundary. A
    band that does hold one has answered for that stretch of the axis:
    a path that cannot reach any of its positions ends there instead of
    reaching around it, so an unrelated mark can neither stand in for a
    missing boundary nor buy a path extra sideways room.

    Returns the deepest position any qualifying path reaches, which is
    what makes the rectangle conservative: a skewed boundary sits
    further in for some bands than others, and cutting at the outermost
    would leave part of the wedge behind.
    """
    edges = [_band_edges(band, window) for band in bands]
    filled = [index for index, found in enumerate(edges) if found]
    if not filled:
        return None
    positions = [edges[index] for index in filled]
    # Bridging skips empty bands only, so the band before a filled one
    # is fixed: its nearest filled predecessor, when close enough.
    steps = [filled[index + 1] - filled[index] for index in range(len(filled) - 1)]
    linked = [step <= _LINEART_TRACK_GAP + 1 for step in steps]

    forward = [[1] * len(row) for row in positions]
    for index in range(1, len(filled)):
        if linked[index - 1]:
            forward[index] = _extend(
                positions[index - 1],
                forward[index - 1],
                positions[index],
                slack * steps[index - 1],
            )
    backward = [[1] * len(row) for row in positions]
    for index in range(len(filled) - 2, -1, -1):
        if linked[index]:
            backward[index] = _extend(
                positions[index + 1],
                backward[index + 1],
                positions[index],
                slack * steps[index],
            )

    needed = _LINEART_BAND_VOTE_SHARE * len(bands)
    supported = [
        position
        for index, row in enumerate(positions)
        for at, position in enumerate(row)
        if forward[index][at] + backward[index][at] - 1 >= needed
    ]
    return max(supported) if supported else None


def _lineart_edge(
    bands: list[list[float]],
    overall: list[float],
    *,
    window: int,
    run: int,
    slack: int,
) -> int | None:
    """Distance from a frame edge to the paper, or ``None`` if unresolved.

    Every profile is indexed outward to inward from its own frame edge,
    so one routine serves all four sides and the caller maps the result
    back. A side resolves only on two pieces of evidence together: one
    coherent boundary within ``window`` of the frame edge that the bands
    agree on (:func:`_boundary_track`), and paper-like ink holding for
    ``run`` positions somewhere behind it.

    The cut falls on the *first* paper-like position past the boundary,
    which is where the paper starts; the sustained run only has to exist
    further in, as proof that real paper lies behind the edge rather
    than more print. Sliding the cut to the run itself would walk it
    through whatever sits in between, and a stamp or a note close to the
    paper edge is exactly what sits there.

    This weighs edge evidence; it cannot recognise content. A dense mark
    spanning most of an axis close to the paper edge is what a boundary
    looks like, and is cropped as one.
    """
    reach = len(overall)
    boundary = _boundary_track(bands, window=window, slack=slack)
    if boundary is None:
        return None
    edge: int | None = None
    count = 0
    for index in range(boundary + 1, reach):
        if overall[index] > _LINEART_PAPER_INK_SHARE:
            count = 0
            continue
        if edge is None:
            edge = index
            if edge >= window:
                return None  # paper only begins past the bounded search
        count += 1
        if count >= run:
            return edge
    return None


def _lineart_bounds(
    raster: bytes, *, width: int, height: int, row_bytes: int, dpi: int
) -> _Bounds | None:
    """Where the paper lies in a native 1-bit raster, or ``None``.

    The scanner thresholded this frame before ScanMole saw it, so there
    is no brightness left to walk: what remains of a paper edge is a
    boundary of ink, dark across most of the perpendicular axis and
    followed by paper. A white margin proves nothing on its own, because
    1-bit padding outside the paper and the page's own white margin are
    the same bits, which is why a dark boundary has to be found first.

    ``None`` means the evidence contradicts itself (a left edge inward
    of the right one); the caller then keeps the frame. An unresolved
    side reports its frame edge, exactly as the brightness walk does.
    """
    pad = 0xFF if width % 8 == 0 else 0xFF ^ (0xFF >> (width % 8))
    window = max(1, round(_LINEART_SEARCH_MM * dpi / 25.4))
    run = max(1, round(_LINEART_PAPER_RUN_MM * dpi / 25.4))
    band_px = max(1, round(_LINEART_BAND_MM * dpi / 25.4))
    slack = max(1, round(_LINEART_TRACK_SKEW * band_px))

    # Sides: profiles over columns, bands over rows. The profile has to
    # reach one run past the window so a boundary at its very inner limit
    # can still prove paper behind it.
    span = min(width, window + run)
    row_bands = _band_bounds(height, band_px)
    heights = [stop - start for start, stop in row_bands]

    def side(first: int, last: int, origin: int, step: int) -> int | None:
        counts = _column_ink(
            raster,
            row_bytes=row_bytes,
            height=height,
            first=first,
            last=last,
            bands=row_bands,
            mask=pad,
        )
        base = origin - first * 8
        shares = [
            [band[base + step * index] / rows for index in range(span)]
            for band, rows in zip(counts, heights, strict=True)
        ]
        totals = [sum(column) for column in zip(*counts, strict=True)]
        overall = [totals[base + step * index] / height for index in range(span)]
        return _lineart_edge(shares, overall, window=window, run=run, slack=slack)

    left = side(0, min(row_bytes, (span + 7) // 8), 0, 1)
    right = side(max(0, (width - span) // 8), row_bytes, width - 1, -1)

    # Ends: profiles over rows, bands over byte columns (one byte spans
    # eight columns, which is finer than any band worth having).
    reach = min(height, window + run)
    column_bands = _band_bounds(row_bytes, max(1, band_px // 8))
    widths = [min(stop * 8, width) - start * 8 for start, stop in column_bands]

    def end(first: int, origin: int, step: int) -> int | None:
        counts = _row_ink(
            raster,
            row_bytes=row_bytes,
            first=first,
            last=first + reach,
            bands=column_bands,
            mask=pad,
        )
        base = origin - first
        shares = [
            [band[base + step * index] / columns for index in range(reach)]
            for band, columns in zip(counts, widths, strict=True)
        ]
        totals = [sum(row) for row in zip(*counts, strict=True)]
        overall = [totals[base + step * index] / width for index in range(reach)]
        return _lineart_edge(shares, overall, window=window, run=run, slack=slack)

    top = end(0, 0, 1)
    bottom = end(height - reach, height - 1, -1)

    bounds = _Bounds(
        left=0 if left is None else left,
        top=0 if top is None else top,
        right=width - 1 if right is None else width - 1 - right,
        bottom=height - 1 if bottom is None else height - 1 - bottom,
    )
    if bounds.left >= bounds.right or bounds.top >= bounds.bottom:
        return None  # contradictory evidence: no plausible paper here
    return bounds


def _autocrop_lineart(path: Path, buffer: bytes, *, dpi: int) -> bool:
    """Crop a native 1-bit ``P4`` frame to its paper boundaries, in place."""
    tokens, offset = read_header(buffer, 2)
    try:
        width, height = int(tokens[0]), int(tokens[1])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    row_bytes = (width + 7) // 8
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = buffer[offset : offset + row_bytes * height]

    bounds = _lineart_bounds(
        raster, width=width, height=height, row_bytes=row_bytes, dpi=dpi
    )
    if bounds is None:
        return False
    # No trim: the frame is already thresholded, so a detected boundary
    # has no half-gray transition to shave, and the walk stops at the
    # first position that reads as paper anyway. Measured over all 63
    # detected sides in the corpus, the outermost line a crop keeps
    # holds at most 4.5% ink, under the share the rule calls paper.
    kept = _finalize(bounds, width, height, 0)
    if kept is None:
        return False
    # The detected bounds are kept to the pixel on every side. Bit-packed
    # rows are repacked rather than sliced from a byte boundary
    # (:func:`~scanmole.pnm.crop_bit_rows`), so a left edge that does not
    # fall on a byte costs nothing; rounding it inward used to give away
    # up to seven columns of paper that the detector had just proved was
    # paper.
    header = b"P4\n%d %d\n" % (
        kept.right - kept.left + 1,
        kept.bottom - kept.top + 1,
    )
    data = crop_bit_rows(
        raster,
        row_bytes=row_bytes,
        box=(kept.left, kept.top, kept.right + 1, kept.bottom + 1),
    )
    replace_file(path, header + data)
    return True


def autocrop_pnm(
    path: Path, trim_px: int, feeder_band_px: int | None = None, *, dpi: int
) -> bool:
    """Crop a raw PNM to the detected paper edges, in place.

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

    Frames a device already thresholded (``P4``) take the ink-density
    path instead (:func:`_lineart_bounds`), which neither ``trim_px`` nor
    ``feeder_band_px`` applies to: there are no transition pixels to
    shave off a binary edge, and mid-gray window padding, the one thing
    the leading-edge band exists for, cannot occur in a 1-bit raster.
    Non-PNM files are left alone.

    Returns:
        Whether the file was rewritten. ``False`` also covers "no backing
        visible" (borderless scan or white backing) and "no paper found" (a
        safety fallback keeping the full frame).

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P":
        return False
    kind = buffer[1:2]
    if kind == b"4":
        return _autocrop_lineart(path, buffer, dpi=dpi)
    if kind not in (b"5", b"6"):
        return False
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
