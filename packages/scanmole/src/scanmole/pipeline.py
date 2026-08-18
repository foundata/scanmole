"""Orchestrate the scan-to-searchable-PDF pipeline.

Stages: acquire pages (scanner or supplied images) -> drop blank pages ->
assemble a PDF -> optionally add an OCR text layer. Each stage emits both a
machine-readable event and a human log line.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.devices import pick_default_device
from scanmole.errors import InputError, NoPagesError, ProcessingError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.external import require_tools
from scanmole.options import is_flatbed_source, parse_page_size
from scanmole.pdf import build_pdf, run_ocr
from scanmole.pnm import (
    adaptive_lineart_threshold,
    autocrop_image,
    binarize_image,
    crop_image,
    image_content_stats,
    image_mean,
)
from scanmole.scanner import EffectiveSettings, scan_to_files
from scanmole.sizing import PageContent, choose_crops

LOGGER = logging.getLogger(__name__)

KeptPage = tuple[int, Path]

_WINDOW_MATCH_MM = 5.0
"""A frame within this of the requested window proves nothing was cropped.

Backends deliver slightly less than the negotiated window (the genesys
backend rounds the 216.7 mm LiDE 220 window down to a 213.4 mm frame), while
real hardware paper detection shortens frames by far more than this.
"""


def analyze_page(
    page: Path,
    number: int,
    config: ScanConfig,
    events: EventWriter,
    mean_hint: float | None = None,
) -> tuple[bool, bool]:
    """Evaluate one page, emit its ``page`` event and log the outcome.

    A page counts as blank when its mean brightness exceeds
    ``config.blank_threshold``; a threshold of ``0`` (or below) disables blank
    detection entirely. Blank pages are dropped unless ``config.keep_blanks``
    is set. ``mean_hint`` replaces the whole-file measurement where the caller
    measured a more meaningful region (the content box of a full-window
    frame, where the surrounding padding would drown a sparse page).

    Returns:
        Whether the page should be kept and whether it was detected as blank.
    """
    mean = mean_hint if mean_hint is not None else image_mean(page)
    keep, blank = blank_verdict(mean, config)
    report_page(page, number, config, events, mean, keep, blank)
    return keep, blank


def blank_verdict(mean: float | None, config: ScanConfig) -> tuple[bool, bool]:
    """The keep/blank decision for a measured mean (pure)."""
    blank = (
        config.blank_threshold > 0
        and mean is not None
        and mean > config.blank_threshold
    )
    return config.keep_blanks or not blank, blank


def report_page(
    page: Path,
    number: int,
    config: ScanConfig,
    events: EventWriter,
    mean: float | None,
    keep: bool,
    blank: bool,
) -> None:
    """Emit the ``page`` event and log line for a decided page."""
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


def publish_pdf(source: Path, output: Path) -> None:
    """Publish ``source`` as ``output``, atomically replacing the reservation.

    The CLI reserved ``output`` as an empty file so concurrent runs cannot
    collide. ``os.replace`` is atomic only within one filesystem and the work
    directory usually lives on another one (/tmp), so the PDF is staged next
    to ``output`` first.

    Raises:
        ProcessingError: If the finished PDF cannot be moved into place.
    """
    staged: Path | None = None
    try:
        handle, staged_name = tempfile.mkstemp(
            dir=output.parent, prefix=f".{output.stem}.", suffix=".part"
        )
        os.close(handle)
        staged = Path(staged_name)
        shutil.move(str(source), str(staged))
        os.replace(staged, output)
    except OSError as exc:
        raise ProcessingError(f"cannot write output {output}: {exc}") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def copy_kept_images(kept: list[KeptPage], destination: Path, stem: str) -> None:
    """Copy each kept page image into a per-batch directory under destination.

    Each batch claims ``<destination>/<stem>/`` (or ``<stem>_2/``, ... when
    taken) with an atomic ``mkdir``: archive directories are typically reused
    across runs, and repeated as well as concurrent batches must not
    overwrite or interleave each other's pages. An output-stem file prefix
    alone cannot guarantee that, because equally named outputs in different
    directories share a stem.

    Raises:
        ProcessingError: If no batch directory could be reserved.
    """
    destination.mkdir(parents=True, exist_ok=True)
    batch_dir: Path | None = None
    for attempt in range(1, 1000):
        candidate = destination / (stem if attempt == 1 else f"{stem}_{attempt}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise ProcessingError(
                f"cannot reserve the archive directory {candidate}: {exc}"
            ) from exc
        batch_dir = candidate
        break
    if batch_dir is None:  # pragma: no cover -- needs 999 same-named batches
        raise ProcessingError(f"cannot reserve an archive directory in {destination}")
    for number, page in kept:
        suffix = page.suffix or ".img"
        shutil.copy2(page, batch_dir / f"page_{number:04d}{suffix}")
    LOGGER.info("Kept page images copied to %s", batch_dir)


def _adopt_adaptive(
    page: Path,
    gray_snapshot: bytes,
    fraction: float,
    measured: list[PageContent],
) -> None:
    """Replace the fixed-0.5 page with the adaptive conversion, best-effort.

    The fixed result on disk stays authoritative until the adaptive one is
    complete (staged sibling, atomic adoption). Recovered strokes may lie
    outside the fixed reach envelope, so the post-conversion envelope is
    unioned into the page's measurement; the robust bbox stays fixed-0.5,
    adaptive pixels never choose the paper size.
    """
    staging = page.with_name(page.name + ".auto")
    try:
        staging.write_bytes(gray_snapshot)
        if not binarize_image(staging, fraction):
            return
        os.replace(staging, page)
    except OSError:
        LOGGER.debug("adaptive conversion abandoned for %s", page, exc_info=True)
        return
    finally:
        staging.unlink(missing_ok=True)
    LOGGER.info(
        "Page %s: faint-original threshold %d%% applied",
        page.name,
        round(fraction * 100),
    )
    if measured and measured[-1].path == page:
        entry = measured[-1]
        stats = image_content_stats(page, min_ink_px=4)
        if stats is not None and stats.reach is not None:
            reach = stats.reach
            if entry.reach_px is not None:
                reach = (
                    min(reach[0], entry.reach_px[0]),
                    min(reach[1], entry.reach_px[1]),
                    max(reach[2], entry.reach_px[2]),
                    max(reach[3], entry.reach_px[3]),
                )
            measured[-1] = dataclasses.replace(entry, reach_px=reach)


def _apply_content_sizes(
    measured: list[PageContent],
    kept: list[KeptPage],
    negotiated: list[EffectiveSettings],
    config: ScanConfig,
    dpi: int | None,
) -> None:
    """Crop full-window frames to their content-decided page sizes.

    Runs after the batch on purpose: the size vote needs every page's content
    box, so a sparse final page in an A4 stack still comes out A4. Only kept
    pages are rewritten; dropped blanks carry no vote (their boxes are empty)
    and never reach the PDF.
    """
    if not measured or dpi is None:
        return
    kept_paths = {page for _, page in kept}
    if not kept_paths.intersection(entry.path for entry in measured):
        return
    source = negotiated[0].source if negotiated else None
    flatbed = (
        is_flatbed_source(source) if source is not None else config.source == "flatbed"
    )
    # Front/back frames of a duplex batch are the same physical sheet and
    # share one paper size. All measured frames enter the decision (dropped
    # blank backsides still pair with their front side); only kept pages are
    # rewritten.
    duplex = "duplex" in (source or config.source or "").lower()
    sized: Counter[str] = Counter()
    for decision in choose_crops(measured, dpi, flatbed, duplex):
        if decision.box_px is None or decision.page.path not in kept_paths:
            continue
        if crop_image(decision.page.path, decision.box_px):
            sized[decision.label] += 1
    if sized:
        summary = ", ".join(
            f"{label} ({count})" for label, count in sorted(sized.items())
        )
        LOGGER.info(
            "Auto page size: no paper edges detectable (white backing); "
            "sized %d page(s) by content: %s",
            sum(sized.values()),
            summary,
        )


def _size_preserved_pages(
    measured: list[PageContent],
    kept: list[KeptPage],
    negotiated: list[EffectiveSettings],
    config: ScanConfig,
) -> None:
    """Best-effort content sizing for pages preserved by a failed run.

    The documented recovery command rebuilds via ``--from-images``, which
    never crops (user-curated inputs), so whatever sizing evidence exists at
    failure time is applied here; otherwise recovery would resurrect the
    full-window frames. Never raises: the original error must propagate.
    """
    try:
        dpi = negotiated[0].resolution if negotiated else None
        _apply_content_sizes(
            measured, kept, negotiated, config, dpi or config.resolution
        )
    except Exception:  # pragma: no cover -- defensive only
        LOGGER.debug("could not size the preserved pages", exc_info=True)


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
    # Validate early, before touching hardware. None means "auto": scan the
    # full device window and crop each page to the detected paper edges.
    auto_page_size = parse_page_size(config.page_size) is None
    # Shave the detected paper box inward by about a third of a millimetre so
    # half-gray edge pixels cannot survive as a dark rim.
    crop_trim_px = max(1, round(config.resolution / 75))

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
            device=device,
            source=config.source,
            mode=config.mode,
            resolution=config.resolution,
            page_size=config.page_size,
            output=str(config.output),
        )

        binarized = False
        negotiated: list[EffectiveSettings] = []
        measured: list[PageContent] = []

        def handle_page(page: Path) -> None:
            # Called per page as it lands: from the scanner's reader thread
            # during a batch, or inline for --from-images. Frontends see the
            # page event while the rest of the batch is still scanning.
            nonlocal total, blanks, binarized
            total += 1
            # With page size auto the scan covers the device's full window;
            # crop to the paper first so backing strips and end-of-paper
            # padding reach neither the 1-bit conversion nor blank detection,
            # and the PDF page gets the paper's real size.
            if not from_images and auto_page_size:
                autocrop_image(page, crop_trim_px)
            # Backends without a 1-bit mode (eSCL offers only Gray/Color)
            # degrade a lineart request to gray; restore the asked-for 1-bit
            # output in software, before blank detection so the 0.995 default
            # keeps its lineart-tuned meaning. --from-images input is
            # user-curated and never touched. In "auto" threshold mode every
            # decision metric still comes from a fixed-0.5 conversion; the
            # gray frame is snapshotted so the guarded adaptive pass can
            # rerun the conversion after the blank verdict.
            gray_snapshot: bytes | None = None
            threshold = config.lineart_threshold
            if not from_images and config.mode == "lineart" and threshold != 0:
                if threshold == "auto":
                    head = page.read_bytes()
                    if head[:2] in (b"P5", b"P6"):
                        gray_snapshot = head
                fixed = 0.5 if threshold == "auto" else threshold
                converted = binarize_image(page, fixed)
                if converted and not binarized:
                    binarized = True
                    LOGGER.info(
                        "Device delivered gray/color pages; converting to "
                        "1-bit lineart in software (threshold %s)",
                        "auto" if threshold == "auto" else f"{threshold:g}",
                    )
            # Judge each axis on its own evidence: an axis still at the scan
            # window is unresolved (white backing, white lid, or a detection
            # that only measures the other axis), a shortened axis is an
            # observed paper extent. Frames with any unresolved axis are
            # measured for the batch-level size decision; frames resolved on
            # both axes are the device's own result and stay untouched.
            mean_hint: float | None = None
            window = negotiated[0].window_mm if negotiated else None
            if not from_images and auto_page_size and window:
                dpi_now = negotiated[0].resolution or config.resolution
                scale = dpi_now / 25.4
                stats = image_content_stats(page, min_ink_px=max(4, round(scale)))
                if stats is not None:
                    unresolved = (
                        stats.frame[0] / scale >= window[0] - _WINDOW_MATCH_MM,
                        stats.frame[1] / scale >= window[1] - _WINDOW_MATCH_MM,
                    )
                    if any(unresolved):
                        measured.append(
                            PageContent(
                                number=total,
                                path=page,
                                frame_px=stats.frame,
                                bbox_px=stats.bbox,
                                reach_px=stats.reach,
                                unresolved=unresolved,
                            )
                        )
                        # Hint only when a content box exists. Without one,
                        # the whole-frame brightness mean must keep deciding:
                        # faint gray content below the ink cutoff (a light
                        # stamp in gray mode) has no box but is not blank.
                        if stats.bbox is not None:
                            mean_hint = stats.mean
            mean = mean_hint if mean_hint is not None else image_mean(page)
            keep, blank = blank_verdict(mean, config)
            # Guarded adaptation only for pages that survive the blank drop:
            # a dropped backside never needs it, a kept faint-only page (via
            # --keep-blanks or --blank-threshold 0) is exactly its use case,
            # and a true blank falls back to the fixed result through the
            # guards. The page event is emitted only after the final
            # conversion, and it carries the fixed-0.5 mean either way.
            if gray_snapshot is not None and keep:
                fraction = adaptive_lineart_threshold(gray_snapshot)
                if fraction is not None:
                    _adopt_adaptive(page, gray_snapshot, fraction, measured)
            report_page(page, total, config, events, mean, keep, blank)
            if blank:
                blanks += 1
            if keep:
                kept.append((total, page))

        dpi: int | None = None
        if config.from_images is not None:
            LOGGER.info("Building PDF from %d image(s) ...", len(config.from_images))
            for image in config.from_images:  # keep the order given
                handle_page(image)
        else:
            if device is None:  # unreachable: resolved above for scan runs
                raise ScanMoleError("no device resolved for scanning")
            if auto_page_size:
                LOGGER.info(
                    "Auto page size: cropping each page to the detected paper edges"
                )
            scanned = scan_to_files(
                config, device, work_dir, events, handle_page, negotiated.append
            )
            # The backend may have snapped the requested dpi; the PDF must be
            # stamped with what the pages were actually scanned at, or their
            # geometry comes out wrong.
            dpi = (
                scanned.settings.resolution
                if scanned.settings.resolution is not None
                else config.resolution
            )

        events.emit("scan_done", total=total, kept=len(kept), blanks=blanks)
        LOGGER.info("Scanned %d page(s), kept %d", total, len(kept))
        _apply_content_sizes(measured, kept, negotiated, config, dpi)
        if config.keep_images is not None:
            copy_kept_images(kept, config.keep_images, config.output.stem)
        if not kept:
            raise NoPagesError(
                f"all {total} page(s) were blank -- nothing to output "
                "(use --keep-blanks to keep them)"
            )

        raw_pdf = work_dir / "raw.pdf"
        build_pdf([page for _, page in kept], raw_pdf, dpi)
        # Deskew cascade, one mechanism per page: the backend where it offers
        # deskew, otherwise ocrmypdf during OCR, otherwise a warning. The
        # request must never be a silent no-op.
        deskew_pending = config.deskew and not (
            negotiated[0].deskew_applied if negotiated else False
        )
        if config.ocr:
            events.emit("ocr_start", lang=config.lang)
            LOGGER.info("Running OCR (%s) ...", config.lang)
            final_pdf = work_dir / "ocr.pdf"
            run_ocr(raw_pdf, final_pdf, config, deskew=deskew_pending)
        else:
            final_pdf = raw_pdf
            if deskew_pending:
                LOGGER.warning(
                    "deskew requested, but the device offers no deskew and "
                    "OCR is off; pages keep their skew (enable --ocr or use "
                    "--no-deskew to silence this)"
                )
        publish_pdf(final_pdf, config.output)

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
            _size_preserved_pages(measured, kept, negotiated, config)
            exc.message += (
                f" -- the {total} scanned page(s) are kept in {work_dir} "
                f"(recover with: scanmole --from-images '{work_dir}'/page_*.pnm "
                "-o out.pdf)"
            )
            exc.args = (exc.message,)
        raise
    except BaseException:
        # Same contract for everything else that can abort a run, including
        # SIGINT/SIGTERM (KeyboardInterrupt, SystemExit) and unexpected bugs:
        # scanned pages must never be deleted before a successful publish.
        if total > 0 and config.from_images is None:
            preserve = True
            _size_preserved_pages(measured, kept, negotiated, config)
            LOGGER.info(
                "The %d scanned page(s) are kept in %s (recover with: "
                "scanmole --from-images '%s'/page_*.pnm -o out.pdf)",
                total,
                work_dir,
                work_dir,
            )
        raise
    finally:
        if not preserve:
            shutil.rmtree(work_dir, ignore_errors=True)


def _check_input_images(images: tuple[Path, ...]) -> None:
    """Validate ``--from-images`` inputs before doing any work.

    Raises:
        InputError: If no images were given (argparse already enforces this
            for CLI calls) or a named image does not exist.
    """
    if not images:
        raise InputError("--from-images needs at least one file")
    for image in images:
        if not image.is_file():
            raise InputError(f"input image not found: {image}")
