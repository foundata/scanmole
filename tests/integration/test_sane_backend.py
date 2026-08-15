"""End-to-end acquisition against the SANE ``test`` backend.

Exercises the real scanimage path (probing, option mapping, the batch loop,
streaming page delivery) with zero hardware. Requires the ``test`` backend to
be enabled in ``/etc/sane.d/dll.conf``; skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scanmole.cli import main

pytestmark = pytest.mark.integration


def _test_backend_available() -> bool:
    if shutil.which("scanimage") is None:
        return False
    try:
        result = subprocess.run(
            ["scanimage", "-f", "%d%n"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.startswith("test:") for line in result.stdout.splitlines())


_NEEDS_TEST_BACKEND = pytest.mark.skipif(
    not _test_backend_available(),
    reason="SANE test backend not enabled (see /etc/sane.d/dll.conf)",
)
_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)


@_NEEDS_TEST_BACKEND
def test_list_devices_reports_the_test_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--list-devices", "--json"]) == 0

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "devices"
    assert any(device["device"].startswith("test:") for device in event["devices"])


@_NEEDS_TEST_BACKEND
@_NEEDS_IMG2PDF
def test_flatbed_scan_produces_a_pdf_and_the_event_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out.pdf"
    # --blank-threshold 0: the backend's synthetic pattern may be near-uniform,
    # and this run tests acquisition, not blank detection.
    argv = [
        "-d",
        "test:0",
        "--source",
        "flatbed",
        "--mode",
        "gray",
        "--no-ocr",
        "--blank-threshold",
        "0",
        "--json",
        "-o",
        str(output),
    ]

    assert main(argv) == 0

    assert output.is_file()
    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    kinds = [event["event"] for event in events]
    assert kinds[0] == "start"
    assert "settings" in kinds
    assert "page" in kinds
    assert kinds[-1] == "done"


@_NEEDS_TEST_BACKEND
@_NEEDS_IMG2PDF
def test_lineart_request_produces_one_bit_pages_on_a_gray_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The test backend offers only Gray and Color, so a lineart request
    # degrades to gray and the pipeline's software binarization must restore
    # 1-bit pages. --keep-images exposes what went into the PDF.
    kept = tmp_path / "kept"
    argv = [
        "-d",
        "test:0",
        "--source",
        "flatbed",
        "--mode",
        "lineart",
        "--no-ocr",
        "--blank-threshold",
        "0",
        "--keep-images",
        str(kept),
        "-o",
        str(tmp_path / "out.pdf"),
    ]

    assert main(argv) == 0

    pages = sorted(kept.glob("page_*.pnm"))
    assert pages
    for page in pages:
        assert page.read_bytes().startswith(b"P4\n")
