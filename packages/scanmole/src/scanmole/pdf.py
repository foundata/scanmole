"""Assemble page images into a PDF and add a searchable text layer."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from scanmole import CREATOR
from scanmole.config import ScanConfig
from scanmole.errors import ProcessingError
from scanmole.external import INSTALL_HINT, TOOL_TIMEOUT_SECONDS, run_command

LOGGER = logging.getLogger(__name__)

PLUGIN_FILE = Path(__file__).with_name("ocrmypdf_plugin.py")
"""The ocrmypdf plugin shipped beside this module (see its docstring)."""

JBIG2_ENCODER = "jbig2"
"""The jbig2enc binary, named as it installs rather than as its project.

Optional and deliberately never required: without it a scan is correct,
only larger. ocrmypdf finds it on ``PATH`` by itself, so nothing here
passes it along; the name exists so both frontends can ask the same
question.
"""


def jbig2_missing() -> bool:
    """Whether the jbig2enc binary is absent from ``PATH``."""
    return shutil.which(JBIG2_ENCODER) is None


def jbig2_would_help(config: ScanConfig) -> bool:
    """Whether installing jbig2enc would shrink this run's output.

    ocrmypdf recodes 1-bit images with jbig2enc during its optimization
    pass, losslessly and often to a fraction of their size, and picks the
    encoder up on its own. So the advice is worth giving only when that
    pass runs at all and the pages it sees are 1-bit: a lineart request
    produces those on every acquisition path except the one that
    deliberately keeps the device's gray output (threshold ``0``).
    Supplied images are whatever the user made them, so the requested
    mode says nothing about them.
    """
    return (
        config.ocr
        and config.optimize > 0
        and config.from_images is None
        and config.mode == "lineart"
        and config.lineart_threshold != 0
        and jbig2_missing()
    )


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
    # img2pdf names itself as the producer, which is right: it wrote the
    # bytes. The creator is the application the document came from.
    command = ["img2pdf", "--creator", CREATOR]
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
        # ocrmypdf rewrites the creator with its own name and drops what
        # the input carried, so ScanMole travels through the plugin hook
        # that composes that value. The plugin runs in ocrmypdf's
        # interpreter, which is why the string comes in as an argument.
        "--plugin",
        str(PLUGIN_FILE),
        "--scanmole-creator",
        CREATOR,
    ]
    if jbig2_would_help(config):
        LOGGER.info(
            "Install jbig2enc for much smaller black and white PDFs; "
            "ocrmypdf uses it automatically once it is on PATH"
        )
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
