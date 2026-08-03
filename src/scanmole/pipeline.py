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


def analyze_page(
    page: Path, number: int, config: ScanConfig, events: EventWriter
) -> tuple[bool, bool]:
    """Evaluate one page, emit its ``page`` event and log the outcome.

    A page counts as blank when its mean brightness exceeds
    ``config.blank_threshold``; a threshold of ``0`` (or below) disables blank
    detection entirely. Blank pages are dropped unless ``config.keep_blanks``
    is set.

    Returns:
        Whether the page should be kept and whether it was detected as blank.
    """
    mean = image_mean(page)
    blank = (
        config.blank_threshold > 0
        and mean is not None
        and mean > config.blank_threshold
    )
    keep = config.keep_blanks or not blank
    events.emit(
        "page",
        n=number,
        file=str(page),
        blank=blank,
        mean=round(mean, 4) if mean is not None else None,
    )
    measured = f"mean {mean:.4f}" if mean is not None else "mean n/a"
    if keep:
        state = "blank, kept" if blank else "kept"
        LOGGER.info("Page %d: %s (%s)", number, state, measured)
    else:
        LOGGER.info("Page %d: blank, dropped (%s)", number, measured)
    return keep, blank


def copy_kept_images(kept: list[KeptPage], destination: Path) -> None:
    """Copy each kept page image into ``destination`` as ``page_NNNN.ext``."""
    destination.mkdir(parents=True, exist_ok=True)
    for number, page in kept:
        suffix = page.suffix or ".img"
        shutil.copy2(page, destination / f"page_{number:04d}{suffix}")
    LOGGER.info("Kept page images copied to %s", destination)


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
    preserve = False
    kept: list[KeptPage] = []
    total = 0
    blanks = 0
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

        def handle_page(page: Path) -> None:
            # Called per page as it lands: from the scanner's reader thread
            # during a batch, or inline for --from-images. Frontends see the
            # page event while the rest of the batch is still scanning.
            nonlocal total, blanks
            total += 1
            keep, blank = analyze_page(page, total, config, events)
            if blank:
                blanks += 1
            if keep:
                kept.append((total, page))

        if config.from_images is not None:
            LOGGER.info("Building PDF from %d image(s) ...", len(config.from_images))
            for image in config.from_images:  # keep the order given
                handle_page(image)
        else:
            if device is None:  # unreachable: resolved above for scan runs
                raise ScanMoleError("no device resolved for scanning")
            scan_to_files(config, device, work_dir, events, handle_page)

        events.emit("scan_done", total=total, kept=len(kept), blanks=blanks)
        LOGGER.info("Scanned %d page(s), kept %d", total, len(kept))
        if config.keep_images is not None:
            copy_kept_images(kept, config.keep_images)
        if not kept:
            raise NoPagesError(
                f"all {total} page(s) were blank -- nothing to output "
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
    except ScanMoleError as exc:
        # The paper has already gone through the feeder and may be unstapled
        # or shredded; once pages exist, they may be the only copy. Keep them
        # and tell the user where they are, whatever went wrong afterwards.
        if total > 0 and config.from_images is None:
            preserve = True
            exc.message += (
                f" -- the {total} scanned page(s) are kept in {work_dir} "
                f"(recover with: scanmole --from-images '{work_dir}'/page_*.pnm "
                "-o out.pdf)"
            )
            exc.args = (exc.message,)
        raise
    finally:
        if not preserve:
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
