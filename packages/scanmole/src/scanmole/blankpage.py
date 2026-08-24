"""Decide whether a page counts as blank, and the one chance to overturn it.

Two rules live here. :func:`blank_verdict` is the blank classification
itself: a measured mean against the configured threshold. :func:`adaptive_outcome`
runs the guarded adaptive 1-bit conversion, which can change that answer,
because a page whose faint strokes vanish at the fixed cut measures blank
and would otherwise be dropped.

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
    image_content_stats,
)
from scanmole.sizing import PageContent

LOGGER = logging.getLogger(__name__)


def blank_verdict(mean: float | None, config: ScanConfig) -> tuple[bool, bool]:
    """The keep/blank decision for a measured mean (pure)."""
    blank = (
        config.blank_threshold > 0
        and mean is not None
        and mean > config.blank_threshold
    )
    return config.keep_blanks or not blank, blank


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
