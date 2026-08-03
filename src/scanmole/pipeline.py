"""Orchestrate the scan-to-searchable-PDF pipeline.

Stages: acquire pages (scanner or supplied images) -> drop blank pages ->
assemble a PDF -> optionally add an OCR text layer. Each stage emits both a
machine-readable event and a human log line.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.devices import pick_default_device
from scanmole.errors import InputError, NoPagesError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.external import require_tools
from scanmole.options import parse_page_size
from scanmole.pdf import build_pdf, run_ocr
from scanmole.pnm import image_mean
from scanmole.scanner import scan_to_files

LOGGER = logging.getLogger(__name__)

KeptPage = tuple[int, Path]


def analyze_pages(
    pages: list[Path], config: ScanConfig, events: EventWriter
) -> tuple[list[KeptPage], int]:
    """Emit a ``page`` event per input and return the pages worth keeping.

    A page counts as blank when its mean brightness exceeds
    ``config.blank_threshold``. Blank pages are dropped unless
    ``config.keep_blanks`` is set.

    Returns:
        The kept pages and the number of pages detected as blank (dropped or
        kept).
    """
    kept: list[KeptPage] = []
    blanks = 0
    for number, page in enumerate(pages, start=1):
        mean = image_mean(page)
        blank = mean is not None and mean > config.blank_threshold
        keep = config.keep_blanks or not blank
        if blank:
            blanks += 1
        events.emit(
            "page",
            n=number,
            file=str(page),
            blank=blank,
            mean=round(mean, 4) if mean is not None else None,
        )
        measured = f"mean {mean:.4f}" if mean is not None else "mean n/a"
        if keep:
            kept.append((number, page))
            state = "blank, kept" if blank else "kept"
            LOGGER.info("Page %d: %s (%s)", number, state, measured)
        else:
            LOGGER.info("Page %d: blank, dropped (%s)", number, measured)
    return kept, blanks


def copy_kept_images(kept: list[KeptPage], destination: Path) -> None:
    """Copy each kept page image into ``destination`` as ``page_NNNN.ext``."""
    destination.mkdir(parents=True, exist_ok=True)
    for number, page in kept:
        suffix = page.suffix or ".img"
        shutil.copy2(page, destination / f"page_{number:04d}{suffix}")
    LOGGER.info("Kept page images copied to %s", destination)


def _acquire_pages(
    config: ScanConfig, device: str | None, work_dir: Path, events: EventWriter
) -> list[Path]:
    """Return the pages to process, either from images or a live scan."""
    if config.from_images is not None:
        pages = list(config.from_images)  # keep the order given
        LOGGER.info("Building PDF from %d image(s) ...", len(pages))
        return pages
    if device is None:  # unreachable: the caller resolves a device for scanning
        raise ScanMoleError("no device resolved for scanning")
    return scan_to_files(config, device, work_dir, events)


def run_pipeline(config: ScanConfig, events: EventWriter) -> int:
    """Run the full pipeline for ``config`` and return the process exit code.

    Raises:
        ScanMoleError: On any acquisition or processing failure. Callers
            translate it into the documented exit code.
    """
    from_images = config.from_images is not None
    required = ["img2pdf"]
    if not from_images:
        required.append("scanimage")
    if config.ocr:
        required.append("ocrmypdf")
    require_tools(required)
    parse_page_size(config.page_size)  # validate early, before touching hardware

    device: str | None = None
    if from_images:
        _check_input_images(config.from_images or ())
    else:
        device = config.device or pick_default_device()

    work_dir = Path(tempfile.mkdtemp(prefix="scanmole-"))
    started = time.monotonic()
    try:
        events.emit(
            "start",
            protocol=1,
            device=device,
            source=config.source,
            mode=config.mode,
            resolution=config.resolution,
            page_size=config.page_size,
            output=str(config.output),
        )
        pages = _acquire_pages(config, device, work_dir, events)

        kept, blanks = analyze_pages(pages, config, events)
        events.emit("scan_done", total=len(pages), kept=len(kept), blanks=blanks)
        LOGGER.info("Scanned %d page(s), kept %d", len(pages), len(kept))
        if config.keep_images is not None:
            copy_kept_images(kept, config.keep_images)
        if not kept:
            raise NoPagesError(
                f"all {len(pages)} page(s) were blank -- nothing to output "
                "(use --keep-blanks to keep them)"
            )

        raw_pdf = work_dir / "raw.pdf"
        dpi = None if from_images else config.resolution
        build_pdf([page for _, page in kept], raw_pdf, dpi)
        if config.ocr:
            events.emit("ocr_start", lang=config.lang)
            LOGGER.info("Running OCR (%s) ...", config.lang)
            run_ocr(raw_pdf, config.output, config)
        else:
            shutil.move(str(raw_pdf), str(config.output))

        events.emit(
            "done",
            output=str(config.output),
            pages=len(kept),
            bytes=config.output.stat().st_size,
            seconds=round(time.monotonic() - started, 2),
        )
        LOGGER.info("Done: %s (%d page(s))", config.output, len(kept))
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _check_input_images(images: tuple[Path, ...]) -> None:
    """Validate ``--from-images`` inputs before doing any work.

    Raises:
        NoPagesError: If no images were given.
        InputError: If a named image does not exist.
    """
    if not images:
        raise NoPagesError("--from-images needs at least one file")
    for image in images:
        if not image.is_file():
            raise InputError(f"input image not found: {image}")
