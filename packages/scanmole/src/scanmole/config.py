"""The scan configuration passed between ScanMole's internal modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LineartThreshold = float | Literal["auto"]
"""Software 1-bit cutoff: a brightness fraction, ``0`` (off) or ``"auto"``
(guarded per-page Otsu threshold; see the software lineart fallback)."""

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
