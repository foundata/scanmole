"""Tests for the img2pdf and ocrmypdf wrappers, with the tools stubbed out."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import ProcessingError
from scanmole.pdf import build_pdf, run_ocr

_CONFIG = ScanConfig(
    device=None,
    source="adf-duplex",
    mode="lineart",
    resolution=300,
    page_size="a4",
    despeckle=1,
    deskew=False,
    crop=False,
    ocr=True,
    lang="deu",
    rotate_pages=True,
    optimize=1,
    pdfa=True,
    blank_threshold=0.995,
    keep_blanks=False,
    from_images=None,
    keep_images=None,
    output=Path("out.pdf"),
)


def _record(
    monkeypatch: pytest.MonkeyPatch, returncode: int = 0, stderr: str = ""
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, timeout_seconds: float, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout="", stderr=stderr
        )

    monkeypatch.setattr("scanmole.pdf.run_command", fake_run)
    return calls


def test_build_pdf_passes_the_resolution_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    build_pdf([Path("a.pnm"), Path("b.pnm")], Path("out.pdf"), dpi=300)

    (command,) = calls
    assert command[:3] == ["img2pdf", "--imgsize", "300dpi"]
    assert command[-2:] == ["-o", "out.pdf"]


def test_build_pdf_omits_imgsize_without_a_dpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    build_pdf([Path("a.png")], Path("out.pdf"), dpi=None)

    assert "--imgsize" not in calls[0]


def test_build_pdf_failure_raises_processing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, returncode=1, stderr="img2pdf: broken input")

    with pytest.raises(ProcessingError, match="img2pdf failed"):
        build_pdf([Path("a.pnm")], Path("out.pdf"), dpi=300)


def test_run_ocr_defaults_to_pdfa_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record(monkeypatch)

    run_ocr(Path("raw.pdf"), Path("out.pdf"), _CONFIG)

    (command,) = calls
    assert command[:3] == ["ocrmypdf", "-l", "deu"]
    assert "--skip-text" in command
    assert "--rotate-pages" in command
    assert "--output-type" not in command


def test_run_ocr_passes_deskew_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)

    run_ocr(Path("raw.pdf"), Path("out.pdf"), _CONFIG)
    run_ocr(Path("raw.pdf"), Path("out.pdf"), _CONFIG, deskew=True)

    without, with_deskew = calls
    assert "--deskew" not in without
    assert "--deskew" in with_deskew


def test_run_ocr_uses_plain_pdf_when_pdfa_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record(monkeypatch)
    config = dataclasses.replace(_CONFIG, pdfa=False, rotate_pages=False)

    run_ocr(Path("raw.pdf"), Path("out.pdf"), config)

    (command,) = calls
    assert ["--output-type", "pdf"] == command[-4:-2]
    assert "--rotate-pages" not in command


def test_run_ocr_failure_hints_at_the_language_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, returncode=5, stderr="Error: tessdata for 'deu' missing")

    with pytest.raises(ProcessingError, match="tesseract-langpack-deu"):
        run_ocr(Path("raw.pdf"), Path("out.pdf"), _CONFIG)
