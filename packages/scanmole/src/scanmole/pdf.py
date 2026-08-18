"""Assemble page images into a PDF and add a searchable text layer."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.errors import ProcessingError
from scanmole.external import INSTALL_HINT, TOOL_TIMEOUT_SECONDS, run_command

LOGGER = logging.getLogger(__name__)


def build_pdf(pages: list[Path], output: Path, dpi: int | None) -> None:
    """Combine ``pages`` into a single PDF with ``img2pdf`` (no re-encoding).

    Args:
        pages: Ordered page images.
        output: Destination PDF path.
        dpi: Resolution to stamp into the PDF. Scanned PNMs carry no DPI
            metadata, so without this ``img2pdf`` would assume 96 dpi.

    Raises:
        ProcessingError: If ``img2pdf`` fails or times out.
    """
    command = ["img2pdf"]
    if dpi is not None:
        command += ["--imgsize", f"{dpi}dpi"]
    command += [str(page) for page in pages]
    command += ["-o", str(output)]
    try:
        result = run_command(command, timeout_seconds=TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError(
            f"img2pdf timed out after {TOOL_TIMEOUT_SECONDS}s"
        ) from exc
    if result.returncode != 0:
        raise ProcessingError(f"img2pdf failed: {result.stderr.strip()}")


def run_ocr(
    source: Path, output: Path, config: ScanConfig, deskew: bool = False
) -> None:
    """Add an OCR text layer to ``source``, writing the result to ``output``.

    Uses ``ocrmypdf`` (Tesseract underneath) with page rotation, optimization
    and idempotent ``--skip-text`` handling. ``deskew`` additionally
    straightens each page (ocrmypdf derives the angle from tesseract); the
    pipeline requests this only when no backend deskew took the job, so a
    page is never resampled twice.

    Raises:
        ProcessingError: If ``ocrmypdf`` fails or times out.
    """
    command = [
        "ocrmypdf",
        "-l",
        config.lang,
        "--skip-text",
        "--optimize",
        str(config.optimize),
    ]
    if deskew:
        command.append("--deskew")
    if config.rotate_pages:
        command.append("--rotate-pages")
    if not config.pdfa:
        command += ["--output-type", "pdf"]
    command += [str(source), str(output)]

    try:
        result = run_command(command, timeout_seconds=TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ProcessingError(
            f"ocrmypdf timed out after {TOOL_TIMEOUT_SECONDS}s"
        ) from exc
    if result.stderr:
        LOGGER.debug("%s", result.stderr.rstrip())
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        needs_langpack = "language" in tail.lower() or "tessdata" in tail.lower()
        hint = f" ({INSTALL_HINT})" if needs_langpack else ""
        raise ProcessingError(
            f"ocrmypdf failed (exit {result.returncode}): {tail}{hint}"
        )
