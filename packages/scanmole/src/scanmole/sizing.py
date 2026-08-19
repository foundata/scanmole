"""Decide page sizes when no paper edge is physically detectable.

White ADF backings and white flatbed lids make the paper boundary invisible:
the padding around the sheet is bit-identical to the sheet's own margin, in
hardware and software alike. What remains detectable is the printed content,
so this module promises conservative content framing, not paper edges. Its
one hard invariant: no detected content, not even the faint kind, ever lies
outside a chosen crop; when sizes and safety conflict, pages come out larger,
never cut.

Decision flow: pages are grouped into physical sheets (front/back pairs on
duplex sources share one paper size, which is physically true rather than a
heuristic), each sheet snaps to the smallest standard size containing its
robust content box, a strict batch majority upgrades sheets whose content
plausibly sits on majority-sized paper (wide enough, fits), and content that
fits no standard, or is receipt-shaped, gets its box plus margins instead.

Pure decision logic; measuring frames and rewriting files stays elsewhere.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scanmole.config import PAGE_SIZES, AutoSizePreference

SNAP_TOLERANCE_MM = 2.0
"""Robust content may exceed a candidate size by this much (skew, edge
artifacts). A classification tolerance only: the final crop is always
expanded to contain all detected content, so it can never cut anything."""

_SNAP_AREA_SLACK = 1.2
"""Candidates within this area factor of the smallest fit count as ties.

Content alone cannot distinguish some paper sizes: almost any A4 page's
content also fits US letter, whose area is 3% smaller. Near-ties resolve by
the explicit family preference (ISO by default), keeping
:data:`~scanmole.config.PAGE_SIZES` table order within a family; a tie set
without a preferred-family member keeps its first candidate."""

_ISO_SIZES = frozenset({"a4", "a5", "a6"})
"""Base names of the ISO A-series entries in the size table; everything
else in the table is the North American family (letter, legal)."""

_ADOPT_WIDTH_FRACTION = 0.7
"""A sheet adopts the majority size only when its content is at least this
fraction of the majority width. Sparse pages of the same paper have full-width
text lines and adopt; receipts and genuinely smaller paper are narrower and
keep their own size, which is what keeps mixed stacks mixed."""

HARDWARE_EXTENT_TOLERANCE_MM = 8.0
"""How far a hardware-shortened axis may overshoot the true paper size.

A resolved axis is an *observation* of the paper extent, not an exact
measurement: the Epson DS-730N delivers A4 sheets as 301.3 to 303.6 mm
frames, up to ~7 mm of backing tail past the 297 mm paper. A standard size
is compatible with an observed extent when it lies within this tolerance
below it (and at most :data:`SNAP_TOLERANCE_MM` above, for jitter)."""

_RECEIPT_MAX_WIDTH_MM = 100.0
_RECEIPT_MIN_ASPECT = 2.2
"""Narrow, tall content (receipt-shaped) skips standard-size snapping:
receipt paper has arbitrary lengths and near-arbitrary widths, and snapping
an 80 mm receipt to A5 would invent 68 mm of white paper."""

FREE_SIDE_MARGIN_MM = 10.0
"""Margin around the content when no standard size applies (receipts)."""

FREE_TAIL_MARGIN_MM = 15.0
"""Margin below the last content row for non-standard feeder pages."""


@dataclass(frozen=True)
class PageContent:
    """One frame's content measurement, in pixels.

    ``bbox_px`` is the robust box (size evidence), ``reach_px`` the permissive
    envelope (crop safety; superset of ``bbox_px`` when both exist).
    """

    number: int
    path: Path
    frame_px: tuple[int, int]
    bbox_px: tuple[int, int, int, int] | None
    reach_px: tuple[int, int, int, int] | None = None
    unresolved: tuple[bool, bool] = (True, True)
    """Per axis (width, height): whether the frame still sits at the scan
    window there. A shortened (resolved) axis carries an observed paper
    extent and is preserved unless a compatible standard size replaces it;
    an unresolved axis is what content sizing is for."""


@dataclass(frozen=True)
class CropDecision:
    """The crop box for one frame; ``None`` keeps the frame as-is."""

    page: PageContent
    box_px: tuple[int, int, int, int] | None
    label: str


@dataclass(frozen=True)
class _Candidate:
    """One standard-size candidate: label, dimensions and paper family.

    The family is explicit metadata so tie breaking never parses labels
    (let alone translated GUI strings).
    """

    label: str
    width: float
    height: float
    family: AutoSizePreference

    @property
    def area(self) -> float:
        return self.width * self.height


def _candidates(frame_mm: tuple[float, float]) -> list[_Candidate]:
    """Standard sizes fitting the frame, in table order.

    Includes landscape orientations (A4 landscape drops out of a 216 mm ADF
    window by itself); duplicate dimensions keep their first label.
    """
    tol = SNAP_TOLERANCE_MM
    seen: set[tuple[float, float]] = set()
    sizes: list[_Candidate] = []
    for name, (width, height) in PAGE_SIZES.items():
        family: AutoSizePreference = "iso" if name in _ISO_SIZES else "north-american"
        for label, dims in (
            (name, (width, height)),
            (f"{name} landscape", (height, width)),
        ):
            if dims in seen:
                continue
            seen.add(dims)
            if dims[0] <= frame_mm[0] + tol and dims[1] <= frame_mm[1] + tol:
                sizes.append(_Candidate(label, dims[0], dims[1], family))
    return sizes


def _contains(candidate: _Candidate, demand_mm: tuple[float, float]) -> bool:
    """Whether the candidate covers the content demand (width, height).

    For feeder sheets the height demand is the content's distance from the
    leading edge (crops are top-anchored there), not the box height: a legal
    sheet whose text starts 20 mm down must not snap to A4 just because the
    text span happens to be shorter than 297 mm.
    """
    tol = SNAP_TOLERANCE_MM
    return (
        candidate.width + tol >= demand_mm[0]
        and candidate.height + tol >= (demand_mm[1])
    )


def _preferred_tie(
    candidates: list[_Candidate], preference: AutoSizePreference
) -> _Candidate:
    """The winner among fitting candidates: smallest area, preference on ties.

    Builds the documented near-tie set (everything within
    :data:`_SNAP_AREA_SLACK` of the smallest area) first, then picks the
    first preferred-family member of the set, keeping table order within
    the family. A set without one keeps its first candidate, so the
    preference can never exclude the other family or override a single
    unambiguous fit.
    """
    smallest = min(entry.area for entry in candidates)
    ties = [entry for entry in candidates if entry.area <= smallest * _SNAP_AREA_SLACK]
    for entry in ties:
        if entry.family == preference:
            return entry
    return ties[0]


def _snap(
    candidates: list[_Candidate],
    demand_mm: tuple[float, float],
    preference: AutoSizePreference,
) -> _Candidate | None:
    """The smallest candidate covering the demand; preference breaks ties."""
    containing = [entry for entry in candidates if _contains(entry, demand_mm)]
    if not containing:
        return None
    return _preferred_tie(containing, preference)


def _unit_extent(
    unit: list[PageContent], scale: float
) -> tuple[float | None, float | None]:
    """The sheet's observed paper extent per axis in mm, or ``None``.

    Duplex sides share the same physical sheet, so the smallest resolved
    observation per axis bounds the paper.
    """
    extents: list[float | None] = []
    for axis in (0, 1):
        observed = [
            page.frame_px[axis] / scale for page in unit if not page.unresolved[axis]
        ]
        extents.append(min(observed) if observed else None)
    return (extents[0], extents[1])


def _extent_compatible(
    candidate: _Candidate,
    extent: tuple[float | None, float | None],
) -> bool:
    """Whether a standard size agrees with the observed paper extents."""
    width, height = candidate.width, candidate.height
    for observed, dimension in ((extent[0], width), (extent[1], height)):
        if observed is None:
            continue
        if not (
            observed - HARDWARE_EXTENT_TOLERANCE_MM
            <= dimension
            <= observed + SNAP_TOLERANCE_MM
        ):
            return False
    return True


def _union(
    boxes: list[tuple[int, int, int, int] | None],
) -> tuple[int, int, int, int] | None:
    present = [box for box in boxes if box is not None]
    if not present:
        return None
    return (
        min(box[0] for box in present),
        min(box[1] for box in present),
        max(box[2] for box in present),
        max(box[3] for box in present),
    )


def _demand_mm(
    bbox: tuple[int, int, int, int], scale: float, flatbed: bool
) -> tuple[float, float]:
    """The (width, height) a size must cover for this content box."""
    height = (bbox[3] - bbox[1]) if flatbed else bbox[3]  # feeder: from row 0
    return ((bbox[2] - bbox[0]) / scale, height / scale)


def _receipt_shaped(bbox: tuple[int, int, int, int], scale: float) -> bool:
    width = (bbox[2] - bbox[0]) / scale
    height = (bbox[3] - bbox[1]) / scale
    return width <= _RECEIPT_MAX_WIDTH_MM and height / width >= _RECEIPT_MIN_ASPECT


def _place(
    size_mm: tuple[float, float],
    page: PageContent,
    dpi: int,
    flatbed: bool,
) -> tuple[int, int, int, int]:
    """Position a target size inside the frame as a pixel box.

    Feeder frames are top-anchored (row 0 is the paper's leading edge) and
    centered on the content horizontally; flatbed frames are centered on the
    content in both axes, because the original sits wherever the user put it.
    The box shifts as needed to keep the content inside, then clamps.
    """
    scale = dpi / 25.4
    frame_w, frame_h = page.frame_px
    box_w = min(frame_w, round(size_mm[0] * scale))
    box_h = min(frame_h, round(size_mm[1] * scale))
    bbox = page.bbox_px

    center_x = (bbox[0] + bbox[2]) / 2 if bbox else frame_w / 2
    x0 = round(center_x - box_w / 2)
    if bbox is not None:  # content first, centering second
        x0 = min(x0, bbox[0])
        x0 = max(x0, bbox[2] - box_w)
    x0 = max(0, min(x0, frame_w - box_w))

    if flatbed:
        center_y = (bbox[1] + bbox[3]) / 2 if bbox else frame_h / 2
        y0 = round(center_y - box_h / 2)
        if bbox is not None:
            y0 = min(y0, bbox[1])
            y0 = max(y0, bbox[3] - box_h)
        y0 = max(0, min(y0, frame_h - box_h))
    else:
        y0 = 0
    return (x0, y0, x0 + box_w, y0 + box_h)


def _conservative_size(
    content: tuple[int, int, int, int] | None,
    extent: tuple[float | None, float | None],
    frames: list[tuple[int, int]],
    dpi: int,
    flatbed: bool,
) -> tuple[int, int]:
    """One physical target size (px) for a sheet no standard size fits.

    Derived once per duplex unit so both sides of the physical sheet get
    the same dimensions: an observed extent constrains its axis for the
    whole sheet, an unobserved axis takes the unit's content (robust
    boxes and reach envelopes united) plus the free margins, and an axis
    with neither falls back to the largest member frame.
    """
    side = round(FREE_SIDE_MARGIN_MM * dpi / 25.4)
    tail = round(FREE_TAIL_MARGIN_MM * dpi / 25.4)
    scale = dpi / 25.4
    if extent[0] is not None:
        width = round(extent[0] * scale)
    elif content is not None:
        width = (content[2] - content[0]) + 2 * side
    else:
        width = max(frame[0] for frame in frames)
    if extent[1] is not None:
        height = round(extent[1] * scale)
    elif content is not None:
        # Feeder sheets are top-anchored: the height runs from the leading
        # edge to the last content row plus the tail margin.
        height = (content[3] - content[1]) + 2 * side if flatbed else content[3] + tail
    else:
        height = max(frame[1] for frame in frames)
    return width, height


def _place_conservative(
    size_px: tuple[int, int], page: PageContent, flatbed: bool
) -> tuple[int, int, int, int]:
    """Place the sheet's conservative size inside one side's frame.

    Sides share dimensions, not raster coordinates: a content-bearing
    side centers on (and never excludes) its own content, a blank side is
    centered deterministically in its frame. Feeder frames stay
    top-anchored.
    """
    frame_w, frame_h = page.frame_px
    box_w = min(frame_w, size_px[0])
    box_h = min(frame_h, size_px[1])
    bbox = page.bbox_px
    if bbox is not None:
        x0 = round((bbox[0] + bbox[2]) / 2 - box_w / 2)
        x0 = min(x0, bbox[0])
        x0 = max(x0, bbox[2] - box_w)
    else:
        x0 = (frame_w - box_w) // 2
    x0 = max(0, min(x0, frame_w - box_w))
    if flatbed:
        if bbox is not None:
            y0 = round((bbox[1] + bbox[3]) / 2 - box_h / 2)
            y0 = min(y0, bbox[1])
            y0 = max(y0, bbox[3] - box_h)
        else:
            y0 = (frame_h - box_h) // 2
        y0 = max(0, min(y0, frame_h - box_h))
    else:
        y0 = 0
    return (x0, y0, x0 + box_w, y0 + box_h)


def _covering(
    box: tuple[int, int, int, int], page: PageContent
) -> tuple[int, int, int, int]:
    """Expand a box to contain all detected content, clamped to the frame.

    Enforces the module invariant: the classification tolerance and the
    permissive reach envelope may exceed the chosen size, and then the page
    comes out larger instead of losing content.
    """
    frame_w, frame_h = page.frame_px
    x0, y0, x1, y1 = box
    for extra in (page.bbox_px, page.reach_px):
        if extra is None:
            continue
        x0, y0 = min(x0, extra[0]), min(y0, extra[1])
        x1, y1 = max(x1, extra[2]), max(y1, extra[3])
    return (max(0, x0), max(0, y0), min(frame_w, x1), min(frame_h, y1))


def choose_crops(
    pages: list[PageContent],
    dpi: int,
    flatbed: bool,
    duplex: bool = False,
    preference: AutoSizePreference = "iso",
) -> list[CropDecision]:
    """Decide every frame's crop from the batch's content measurements.

    Args:
        pages: Content measurements of the qualifying frames, in scan order.
        dpi: The resolution the frames were scanned at.
        flatbed: Whether the frames came from a flatbed (changes placement).
        duplex: Whether consecutive frames are front/back of the same sheet
            (they then share one size decision).
        preference: Paper family that wins content-only near-ties (A4
            versus Letter is indistinguishable from most content); never a
            restriction, and overruled by content bounds and observed
            hardware extents.

    Returns:
        One decision per input page, in order. ``box_px`` is ``None`` when the
        frame should stay untouched (no content and no batch majority, or the
        decision equals the full frame anyway).
    """
    scale = dpi / 25.4
    units: list[list[PageContent]] = []
    if duplex:
        # Pair by page number, not list position: a sheet is (2k-1, 2k) in
        # delivery order, and pairing must survive gaps (frames that did not
        # qualify for measuring).
        sheets: dict[int, list[PageContent]] = {}
        for page in pages:
            sheets.setdefault((page.number + 1) // 2, []).append(page)
        units = [sheets[sheet] for sheet in sorted(sheets)]
    else:
        units = [[page] for page in pages]

    unit_boxes = [_union([page.bbox_px for page in unit]) for unit in units]
    unit_extents = [_unit_extent(unit, scale) for unit in units]
    snapped: list[_Candidate | None] = []
    pinned: list[bool] = []
    for unit, bbox, extent in zip(units, unit_boxes, unit_extents, strict=True):
        frame = unit[0].frame_px
        frame_mm = (frame[0] / scale, frame[1] / scale)
        has_extent = any(observed is not None for observed in extent)
        # Observed extents are stronger evidence than content: they come
        # from an actual edge detection and filter the candidates first.
        options = [
            candidate
            for candidate in _candidates(frame_mm)
            if _extent_compatible(candidate, extent)
        ]
        candidate: _Candidate | None = None
        if bbox is not None:
            # The receipt shape rule guards against inventing paper from
            # content alone; only an observed *width* overrules it, since a
            # height observation (hardware lower-edge detection) says
            # nothing about how wide the paper is. Evidence constrains
            # axes independently.
            if extent[0] is not None or not _receipt_shaped(bbox, scale):
                candidate = _snap(options, _demand_mm(bbox, scale, flatbed), preference)
        elif has_extent and options:
            # A blank sheet with an observed extent: the smallest size the
            # observation allows, family preference on near-ties.
            candidate = _preferred_tie(options, preference)
        snapped.append(candidate)
        pinned.append(has_extent and candidate is not None)

    # Strict majority over the sheets that carry content: a plurality must
    # not rewrite a genuinely mixed stack (50/50 stays 50/50).
    voters = sum(1 for bbox in unit_boxes if bbox is not None)
    votes = Counter(entry for entry in snapped if entry is not None)
    majority: _Candidate | None = None
    if votes:
        top = max(votes, key=lambda entry: (votes[entry], entry.area))
        if votes[top] * 2 > voters:
            majority = top

    decisions: list[CropDecision] = []
    for unit, bbox, extent, own, pin in zip(
        units, unit_boxes, unit_extents, snapped, pinned, strict=True
    ):
        chosen = own
        # A size established by an observed extent is a per-sheet fact; the
        # batch majority only fills in where the evidence was content alone,
        # and never against a sheet's observed extent.
        if majority is not None and not pin and _extent_compatible(majority, extent):
            if bbox is None:
                if chosen is None:
                    chosen = majority  # blank sheet: majority is the best guess
            elif chosen != majority and _contains(
                majority, _demand_mm(bbox, scale, flatbed)
            ):
                # Upgrade only when the content is wide enough to plausibly
                # sit on majority-sized paper; narrower paper (receipts,
                # smaller formats) keeps its own decision.
                width_mm = (bbox[2] - bbox[0]) / scale
                if width_mm >= _ADOPT_WIDTH_FRACTION * majority.width:
                    chosen = majority
        has_extent = any(observed is not None for observed in extent)
        conservative: tuple[int, int] | None = None
        if chosen is None and (bbox is not None or has_extent):
            # No standard size fits the evidence: one conservative target
            # size for the whole physical sheet, from the unit's content
            # union (robust and reach), observed extents and free margins,
            # so a strip's blank back cannot stay at full window width
            # while its front is content-framed.
            content = _union([bbox] + [page.reach_px for page in unit])
            conservative = _conservative_size(
                content, extent, [page.frame_px for page in unit], dpi, flatbed
            )
        for page in unit:
            if chosen is not None:
                box = _place((chosen.width, chosen.height), page, dpi, flatbed)
                label = chosen.label
            elif conservative is not None:
                box = _place_conservative(conservative, page, flatbed)
                label = "content"
            else:
                decisions.append(CropDecision(page=page, box_px=None, label="frame"))
                continue
            box = _covering(box, page)
            if box == (0, 0, page.frame_px[0], page.frame_px[1]):
                decisions.append(CropDecision(page=page, box_px=None, label=label))
            else:
                decisions.append(CropDecision(page=page, box_px=box, label=label))
    return decisions
