"""The scan configuration passed between ScanMole's internal modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LineartThreshold = float | Literal["auto"]
"""Software 1-bit cutoff: a brightness fraction, ``0`` (off) or ``"auto"``
(guarded per-page Otsu threshold; see the software lineart fallback)."""

AutoSizePreference = Literal["iso", "north-american"]
"""Paper family that wins content-only near-ties in automatic page sizing."""

DeskewMethod = Literal["auto", "scanmole", "scanner"]
"""Who straightens a skewed page, when deskew is requested at all.

``auto`` (the default) lets ScanMole choose, which today means its own
host path on every device that can be told not to deskew, since no
backend mechanism has passed the qualification gate; the exception is a
scanner whose deskew is read-only and already on, which owns the request
because nothing can stop it. ``scanmole`` and ``scanner`` demand one
owner and refuse the scan rather than silently falling back to the
other. Orthogonal to the ``deskew`` switch, which decides whether
anything straightens at all."""

SheetFlow = Literal["single", "stack", "collect"]
"""How many physical sheets one run acquires.

``single`` scans one sheet, ``stack`` (the default) drains the loaded
feeder once, ``collect`` keeps one run open across multiple scanner
invocations until the collection is explicitly finished."""

PAGE_SIZES: dict[str, tuple[float, float]] = {
    # name -> (width_mm, height_mm)
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "a6": (105.0, 148.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}


@dataclass(frozen=True)
class ScanConfig:
    """A fully resolved scan request.

    The CLI builds this from parsed arguments so the pipeline works with a
    typed record instead of an untyped argument namespace. ``output`` is the
    final, de-duplicated destination path; ``from_images`` being non-``None``
    selects the scanner-free path.
    """

    device: str | None
    source: str
    mode: str
    resolution: int
    page_size: str
    despeckle: int
    deskew: bool
    crop: bool
    ocr: bool
    lang: str
    rotate_pages: bool
    optimize: int
    pdfa: bool
    blank_threshold: float
    keep_blanks: bool
    from_images: tuple[Path, ...] | None
    keep_images: Path | None
    output: Path
    # Defaulted so the record stays constructible from older call sites.
    lineart_threshold: LineartThreshold = 0.5
    deskew_method: DeskewMethod = "auto"
    """Which mechanism owns the deskew request; ignored while ``deskew``
    is off, but still carried so turning deskew back on keeps the choice."""
    auto_size_preference: AutoSizePreference = "iso"
    """Tie-break family for ambiguous automatic page sizes (never a
    restriction: either family stays selectable by the evidence)."""
    sheet_flow: SheetFlow = "stack"
    """How many physical sheets this run acquires (see :data:`SheetFlow`)."""
