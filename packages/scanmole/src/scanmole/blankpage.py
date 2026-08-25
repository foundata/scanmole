"""Decide whether a page counts as blank, and the one chance to overturn it.

Three rules live here. :func:`blank_verdict` is the blank classification
itself: a measured mean against the configured threshold. :func:`adaptive_outcome`
runs the guarded adaptive 1-bit conversion, which can change that answer,
because a page whose faint strokes vanish at the fixed cut measures blank
and would otherwise be dropped. :func:`sparse_rescue` is the second and
last chance of every other dropped page: localized coherent print-shaped
ink can overturn a whole-page mean its own sparseness diluted, without
touching the raster or its measured geometry.

The fixed-0.5 result on disk stays authoritative throughout: the adaptive
candidate is staged as a sibling, inspected, and either adopted atomically
or discarded, so no failure between staging, inspection and adoption can
cost the page. Nothing here emits an event or knows about the batch; the
caller in :mod:`scanmole.pipeline` owns both.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.pnm import (
    CoherentInk,
    adaptive_lineart_threshold,
    binarize_image,
    coherent_ink,
    coherent_regions,
    image_content_stats,
    pnm_evidence_mask,
    read_header,
)
from scanmole.sizing import PageContent

LOGGER = logging.getLogger(__name__)

_RESCUE_INK_CUTOFF = 0.5
"""Gray fraction below which a pixel counts as rescue-evidence ink.

The same fixed cut the software lineart conversion and content
detection use: rescue evidence asks what a print actually deposited,
not what an adaptive threshold could still recover."""

_RESCUE_MAX_SPAN = 0.9
"""A region spanning at least this fraction of a frame axis is not
localized content. Scanner boundary bars run the full height and
trailing-edge shadows the full width of the frame; printed words,
lines and marks never come close on either axis."""

_RESCUE_MAX_ASPECT = 12.0
"""Reject regions longer than this many times their own thickness.

A boundary bar or shadow band that survived the span rule is still a
hairline: metres of length against millimetres of thickness. Words,
word groups, stamps and paragraphs stay far below this; a lone
full-width rule is deliberately given up as indistinguishable from a
scanner artifact."""

_RESCUE_MIN_MM = 2.5
"""Reject regions thinner than this on either axis.

Print has glyph height: real words and marks measured 3 mm and taller
on every corpus. A trailing-edge shadow that breaks into short
fragments produces regions exactly two analysis tiles (about 2 mm)
tall, whatever their length, and stays under this floor."""

_RESCUE_SOLID_MEAN = 0.55
"""Reject regions darker than this inside their own box.

Print is strokes: measured word regions keep 69 to 79 percent of
their box white. A punch hole on a blank back is a filled blob at 31
to 40 percent and no stroke of text comes near it; a solid stamp or
logo is deliberately given up as indistinguishable from one."""


def blank_verdict(mean: float | None, config: ScanConfig) -> tuple[bool, bool]:
    """The keep/blank decision for a measured mean (pure)."""
    blank = (
        config.blank_threshold > 0
        and mean is not None
        and mean > config.blank_threshold
    )
    return config.keep_blanks or not blank, blank


def _localized(region: CoherentInk, frame: tuple[int, int], dpi: int) -> bool:
    """Whether one coherent region is localized printed content.

    Axis-spanning regions, hairlines, sub-glyph slivers and filled
    blobs are scanner artifacts (boundary bars, shadow fragments,
    punch holes) however coherent they are; only a region bounded on
    both axes and plausibly shaped and filled like print counts as
    rescue evidence.
    """
    width = region.box[2] - region.box[0]
    height = region.box[3] - region.box[1]
    if width >= frame[0] * _RESCUE_MAX_SPAN or height >= frame[1] * _RESCUE_MAX_SPAN:
        return False
    if max(width, height) > min(width, height) * _RESCUE_MAX_ASPECT:
        return False
    minimum = _RESCUE_MIN_MM * dpi / 25.4
    if width < minimum or height < minimum:
        return False
    return region.mean >= _RESCUE_SOLID_MEAN


def sparse_rescue(
    page: Path,
    verdict: tuple[bool, bool],
    mean: float | None,
    config: ScanConfig,
    dpi: int,
) -> tuple[bool, bool, float | None]:
    """One guarded second look at a page the mean threshold would drop.

    Sparse but genuine content dilutes to nothing over a whole page: one
    printed line on A4 reads above the default threshold, and a correct
    (larger) page geometry only raises the mean further. When the primary
    verdict would drop the page, an in-memory ink mask at the fixed cut
    is searched for locally coherent, localized print-shaped regions;
    their aggregate brightness must itself pass the configured threshold.
    Axis-spanning boundary bars, trailing-edge shadows and hairline
    streaks never qualify (:func:`_localized`), scattered noise and
    backing texture never reach tile coherence, and a page without any
    such evidence stays exactly as decided. The raster on disk is never
    touched, and nothing about the page's measured geometry changes.
    """
    keep, blank = verdict
    if keep or config.blank_threshold <= 0:
        return keep, blank, mean
    try:
        buffer = page.read_bytes()
        mask = pnm_evidence_mask(buffer, _RESCUE_INK_CUTOFF)
        if mask is None:
            return keep, blank, mean
        tokens, _offset = read_header(mask, 2)
        frame = (int(tokens[0]), int(tokens[1]))
        regions = coherent_regions(mask, dpi)
    except (ValueError, OSError):
        LOGGER.debug("unreadable rescue evidence for %s", page, exc_info=True)
        return keep, blank, mean
    localized = [region for region in regions if _localized(region, frame, dpi)]
    if not localized:
        return keep, blank, mean
    area = sum(
        (region.box[2] - region.box[0]) * (region.box[3] - region.box[1])
        for region in localized
    )
    ink = sum(
        (1.0 - region.mean)
        * (region.box[2] - region.box[0])
        * (region.box[3] - region.box[1])
        for region in localized
    )
    evidence_mean = min(1.0, max(0.0, 1.0 - ink / area))
    if blank_verdict(evidence_mean, config)[1]:
        return keep, blank, mean
    LOGGER.info(
        "Page %s: blank at the whole-page mean, kept for localized "
        "coherent content (%d region(s))",
        page.name,
        len(localized),
    )
    return True, False, evidence_mean


def _stage_adaptive(page: Path, gray_snapshot: bytes, fraction: float) -> Path | None:
    """Prepare the adaptive 1-bit candidate as a staged sibling of ``page``.

    The fixed result on disk stays authoritative; the caller inspects the
    candidate and either adopts it atomically or discards it. Returns
    ``None`` (after cleanup) when writing or converting fails.
    """
    staging = page.with_name(page.name + ".auto")
    try:
        staging.write_bytes(gray_snapshot)
        if binarize_image(staging, fraction):
            return staging
    except OSError:
        LOGGER.debug("adaptive candidate abandoned for %s", page, exc_info=True)
    staging.unlink(missing_ok=True)
    return None


def _adopt_candidate(staging: Path, page: Path) -> bool:
    """Atomically replace the fixed page with the candidate, best-effort."""
    try:
        os.replace(staging, page)
    except OSError:
        LOGGER.debug("adaptive adoption failed for %s", page, exc_info=True)
        return False
    return True


def _union_box(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _extend_reach(page: Path, measured: list[PageContent]) -> None:
    """Union the adopted page's reach envelope into its measurement.

    Recovered strokes may lie outside the fixed reach envelope and must not
    be cropped; the robust bbox stays the fixed-0.5 one, so adaptive pixels
    never choose the paper size of a page the fixed verdict already kept.
    """
    if not measured or measured[-1].path != page:
        return
    stats = image_content_stats(page, min_ink_px=4)
    if stats is None or stats.reach is None:
        return
    entry = measured[-1]
    reach = stats.reach
    if entry.reach_px is not None:
        reach = _union_box(reach, entry.reach_px)
    measured[-1] = dataclasses.replace(entry, reach_px=reach)


def _apply_rescue_measurement(
    page: Path, measured: list[PageContent], evidence: CoherentInk
) -> None:
    """Make the coherent box a rescued page's sizing evidence.

    The fixed measurement of a rescued page saw a blank frame, so the
    coherent box is the only robust evidence of where its content sits.
    The adopted page's permissive envelope and the box itself are unioned
    into the reach so recovered strokes cannot be cropped.
    """
    if not measured or measured[-1].path != page:
        return
    entry = measured[-1]
    reach = evidence.box
    stats = image_content_stats(page, min_ink_px=4)
    if stats is not None and stats.reach is not None:
        reach = _union_box(reach, stats.reach)
    if entry.reach_px is not None:
        reach = _union_box(reach, entry.reach_px)
    measured[-1] = dataclasses.replace(entry, bbox_px=evidence.box, reach_px=reach)


def _rescue_evidence(staging: Path, dpi: int) -> CoherentInk | None:
    """Best-effort coherence measurement of the candidate; never raises."""
    try:
        return coherent_ink(staging.read_bytes(), dpi)
    except (ValueError, OSError):
        LOGGER.debug("unreadable rescue candidate %s", staging, exc_info=True)
        return None


def adaptive_outcome(
    page: Path,
    gray_snapshot: bytes,
    verdict: tuple[bool, bool],
    mean: float | None,
    measured: list[PageContent],
    config: ScanConfig,
    dpi: int,
) -> tuple[bool, bool, float | None]:
    """Run the guarded adaptation and return the final ``(keep, blank, mean)``.

    A page the fixed verdict keeps adopts an accepted candidate best-effort,
    with the fixed mean and verdict untouched (this includes blank pages
    kept via ``--keep-blanks``: the user keeps every page, so no rescue
    evidence is required). A dropped fixed-blank gets one rescue chance: the
    candidate must additionally show locally coherent text-like ink whose
    region mean passes the configured blank threshold, because the Otsu
    guards alone accept distributed bimodal noise. Every failure (staging,
    coherence, adoption) leaves the fixed page, verdict and mean standing.
    """
    keep, blank = verdict
    fraction = adaptive_lineart_threshold(gray_snapshot)
    if fraction is None:
        return keep, blank, mean
    staging = _stage_adaptive(page, gray_snapshot, fraction)
    if staging is None:
        return keep, blank, mean
    try:
        if keep:
            if _adopt_candidate(staging, page):
                LOGGER.info(
                    "Page %s: faint-original threshold %d%% applied",
                    page.name,
                    round(fraction * 100),
                )
                _extend_reach(page, measured)
            return keep, blank, mean
        evidence = _rescue_evidence(staging, dpi)
        if evidence is None or blank_verdict(evidence.mean, config)[1]:
            return keep, blank, mean
        if not _adopt_candidate(staging, page):
            return keep, blank, mean
        LOGGER.info(
            "Page %s: blank at the fixed threshold, rescued by coherent "
            "faint content (threshold %d%%)",
            page.name,
            round(fraction * 100),
        )
        _apply_rescue_measurement(page, measured, evidence)
        return True, False, evidence.mean
    finally:
        staging.unlink(missing_ok=True)
