"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import NoPagesError
from scanmole.events import EventWriter
from scanmole.pipeline import run_pipeline

pytestmark = pytest.mark.integration

_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)


def _gray_page(path: Path) -> Path:
    path.write_bytes(b"P5\n4 4\n255\n" + bytes([120] * 16))
    return path


def _white_page(path: Path) -> Path:
    path.write_bytes(b"P5\n4 4\n255\n" + bytes([255] * 16))
    return path


def _config(images: tuple[Path, ...], output: Path) -> ScanConfig:
    return ScanConfig(
        device=None,
        source="adf-duplex",
        mode="lineart",
        resolution=300,
        page_size="a4",
        despeckle=1,
        deskew=False,
        crop=False,
        ocr=False,
        lang="deu",
        rotate_pages=True,
        optimize=1,
        pdfa=False,
        blank_threshold=0.995,
        keep_blanks=False,
        from_images=images,
        keep_images=None,
        output=output,
    )


@_NEEDS_IMG2PDF
def test_from_images_drops_blank_and_builds_pdf(tmp_path: Path) -> None:
    kept = _gray_page(tmp_path / "page1.pgm")
    blank = _white_page(tmp_path / "page2.pgm")
    output = tmp_path / "result.pdf"
    stream = io.StringIO()

    exit_code = run_pipeline(
        _config((kept, blank), output), EventWriter(enabled=True, stream=stream)
    )

    assert exit_code == 0
    assert output.is_file()
    assert output.stat().st_size > 0

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    kinds = [event["event"] for event in events]
    assert kinds == ["start", "page", "page", "scan_done", "done"]

    scan_done = next(event for event in events if event["event"] == "scan_done")
    assert scan_done == {"event": "scan_done", "total": 2, "kept": 1}
    done = next(event for event in events if event["event"] == "done")
    assert done["pages"] == 1


@_NEEDS_IMG2PDF
def test_from_images_all_blank_returns_no_pages_code(tmp_path: Path) -> None:
    blank = _white_page(tmp_path / "blank.pgm")
    output = tmp_path / "out.pdf"

    with pytest.raises(NoPagesError):
        run_pipeline(_config((blank,), output), EventWriter(enabled=False))

    assert not output.exists()
