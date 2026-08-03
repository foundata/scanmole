"""Assemble page images into a PDF and add a searchable text layer."""

from __future__ import annotations

import logging
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.errors import ScanMoleError
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
        ScanMoleError: If ``img2pdf`` fails.
    """
    command = ["img2pdf"]
    if dpi is not None:
        command += ["--imgsize", f"{dpi}dpi"]
    command += [str(page) for page in pages]
    command += ["-o", str(output)]
    result = run_command(command, timeout_seconds=TOOL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ScanMoleError(f"img2pdf failed: {result.stderr.strip()}")


def run_ocr(source: Path, output: Path, config: ScanConfig) -> None:
    """Add an OCR text layer to ``source``, writing the result to ``output``.

    Uses ``ocrmypdf`` (Tesseract underneath) with page rotation, optimization
    and idempotent ``--skip-text`` handling.

    Raises:
        ScanMoleError: If ``ocrmypdf`` fails.
    """
    command = [
        "ocrmypdf",
        "-l",
        config.lang,
        "--skip-text",
        "--optimize",
        str(config.optimize),
    ]
    if config.rotate_pages:
        command.append("--rotate-pages")
    if not config.pdfa:
        command += ["--output-type", "pdf"]
    command += [str(source), str(output)]

    result = run_command(command, timeout_seconds=TOOL_TIMEOUT_SECONDS)
    if result.stderr:
        LOGGER.debug("%s", result.stderr.rstrip())
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        needs_langpack = "language" in tail.lower() or "tessdata" in tail.lower()
        hint = f" ({INSTALL_HINT})" if needs_langpack else ""
        raise ScanMoleError(f"ocrmypdf failed (exit {result.returncode}): {tail}{hint}")
