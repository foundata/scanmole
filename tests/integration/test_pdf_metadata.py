"""The Creator the produced PDFs actually carry (real img2pdf/ocrmypdf).

Reads the metadata out of the finished files rather than trusting the
command line, because ocrmypdf composes ``/Creator`` itself and only a
real run shows what survives.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scanmole import CREATOR
from scanmole.config import ScanConfig
from scanmole.pdf import build_pdf, run_ocr

pytestmark = pytest.mark.integration

_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)
_NEEDS_OCRMYPDF = pytest.mark.skipif(
    shutil.which("ocrmypdf") is None or shutil.which("tesseract") is None,
    reason="ocrmypdf and tesseract are not installed",
)


def _blank_page(directory: Path) -> Path:
    page = directory / "page.pnm"
    page.write_bytes(b"P5\n200 280\n255\n" + bytes([255] * (200 * 280)))
    return page


def _creator(pdf: Path) -> str:
    """The ``/Creator`` string, read without a PDF library."""
    raw = pdf.read_bytes()
    match = re.search(rb"/Creator\s*\(((?:[^()\\]|\\.)*)\)", raw)
    if match is not None:
        return match.group(1).decode("latin-1")
    # ocrmypdf writes it into an XMP packet as well.
    xmp = re.search(rb"<xmp:CreatorTool>([^<]*)</xmp:CreatorTool>", raw)
    return xmp.group(1).decode("utf-8") if xmp is not None else ""


def _config(tmp_path: Path) -> ScanConfig:
    return ScanConfig(
        device=None,
        source="adf",
        mode="gray",
        resolution=300,
        page_size="a4",
        despeckle=1,
        deskew=False,
        crop=False,
        ocr=True,
        lang="eng",
        rotate_pages=False,
        optimize=1,
        pdfa=True,
        blank_threshold=0.995,
        keep_blanks=False,
        from_images=None,
        keep_images=None,
        output=tmp_path / "out.pdf",
    )


@_NEEDS_IMG2PDF
def test_a_pdf_without_ocr_names_scanmole_alone(tmp_path: Path) -> None:
    output = tmp_path / "plain.pdf"

    build_pdf([_blank_page(tmp_path)], output, dpi=300)

    assert _creator(output) == CREATOR


@_NEEDS_IMG2PDF
@_NEEDS_OCRMYPDF
def test_ocr_keeps_scanmole_behind_the_tool_that_rewrote_the_file(
    tmp_path: Path,
) -> None:
    # ocrmypdf always prepends itself and discards the incoming creator,
    # so second position is the best available; being absent would be the
    # alternative.
    plain = tmp_path / "plain.pdf"
    build_pdf([_blank_page(tmp_path)], plain, dpi=300)
    output = tmp_path / "ocred.pdf"

    run_ocr(plain, output, _config(tmp_path))

    creator = _creator(output)
    assert creator.startswith("OCRmyPDF ")
    assert CREATOR in creator
    assert (
        creator.index("OCRmyPDF") < creator.index(CREATOR) < creator.index("Tesseract")
    )


@_NEEDS_IMG2PDF
@_NEEDS_OCRMYPDF
def test_a_plugin_that_cannot_load_leaves_the_scan_working(
    tmp_path: Path,
) -> None:
    # The engine subclass reaches into ocrmypdf's builtin plugins. If a
    # future release moves it, the guard has to cost the metadata line
    # rather than the scan.
    broken = tmp_path / "broken_plugin.py"
    broken.write_text(
        "try:\n"
        "    from ocrmypdf.builtin_plugins.tesseract_ocr import Gone\n"
        "except ImportError:\n"
        "    pass\n"
    )
    plain = tmp_path / "plain.pdf"
    build_pdf([_blank_page(tmp_path)], plain, dpi=300)
    output = tmp_path / "ocred.pdf"

    result = subprocess.run(
        [
            "ocrmypdf",
            "-l",
            "eng",
            "--skip-text",
            "--plugin",
            str(broken),
            str(plain),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-400:]
    assert CREATOR not in _creator(output)  # degraded, not broken
