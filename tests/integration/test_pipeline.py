"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import NoPagesError, ProcessingError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.pipeline import analyze_page, publish_pdf, run_pipeline
from scanmole.scanner import EffectiveSettings, ScanResult

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


def _config(images: tuple[Path, ...] | None, output: Path) -> ScanConfig:
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
    assert scan_done == {"event": "scan_done", "total": 2, "kept": 1, "blanks": 1}
    start = next(event for event in events if event["event"] == "start")
    assert start["protocol"] == 1
    assert start["source"] == "adf-duplex"
    done = next(event for event in events if event["event"] == "done")
    assert done["pages"] == 1
    assert done["bytes"] > 0


@_NEEDS_IMG2PDF
def test_from_images_all_blank_returns_no_pages_code(tmp_path: Path) -> None:
    blank = _white_page(tmp_path / "blank.pgm")
    output = tmp_path / "out.pdf"

    with pytest.raises(NoPagesError):
        run_pipeline(_config((blank,), output), EventWriter(enabled=False))

    assert not output.exists()


def test_processing_failure_preserves_scanned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        return ScanResult(
            pages=[page],
            settings=EffectiveSettings(source=None, mode=None, resolution=None),
        )

    def failing_build_pdf(pages: object, output: Path, dpi: object) -> None:
        raise ProcessingError("img2pdf failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build_pdf)
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(ProcessingError) as info:
        run_pipeline(config, EventWriter(enabled=False))

    message = info.value.message
    assert "kept in" in message
    work_dir = Path(message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        assert (work_dir / "page_0001.pnm").is_file()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_page_callback_failure_preserves_pages_and_reports_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors run_scanimage's contract: a failing page callback terminates the
    # batch and propagates as a ScanMoleError after pages were delivered.
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise ScanMoleError("page processing failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    stream = io.StringIO()
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(config, EventWriter(enabled=True, stream=stream))

    message = info.value.message
    assert "kept in" in message
    work_dir = Path(message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        assert (work_dir / "page_0001.pnm").is_file()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    kinds = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert "scan_done" not in kinds
    assert "done" not in kinds


def test_from_images_failure_does_not_claim_preserved_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _gray_page(tmp_path / "input.pgm")

    def failing_build_pdf(pages: object, output: Path, dpi: object) -> None:
        raise ProcessingError("img2pdf failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build_pdf)

    with pytest.raises(ProcessingError) as info:
        run_pipeline(_config((page,), tmp_path / "out.pdf"), EventWriter(enabled=False))

    assert "kept in" not in info.value.message


def test_snapped_resolution_reaches_pdf_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The device offers 150 and 600 dpi; the requested 300 dpi snaps to 150,
    # and img2pdf must be told the dpi the pages were actually scanned at.
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}

    def fake_run_scanimage(command: list[str], on_page: object) -> tuple[int, str]:
        assert "--resolution" in command
        assert command[command.index("--resolution") + 1] == "150"
        batch = next(a for a in command if a.startswith("--batch=")).split("=", 1)[1]
        page = _gray_page(Path(batch % 1))
        assert callable(on_page)
        on_page(page)
        return 7, ""

    stamped: list[object] = []

    def fake_build_pdf(pages: object, output: Path, dpi: object) -> None:
        stamped.append(dpi)
        output.write_bytes(b"%PDF-fake")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.scanner.probe_capabilities", lambda device: caps)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run_scanimage)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", fake_build_pdf)
    stream = io.StringIO()
    config = _config(images=None, output=tmp_path / "out.pdf")

    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0

    assert stamped == [150]
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    settings = next(event for event in events if event["event"] == "settings")
    assert settings["resolution"] == 150
    start = next(event for event in events if event["event"] == "start")
    assert start["resolution"] == 300  # the requested value, per the contract


def test_publish_pdf_replaces_the_reservation_and_leaves_no_staging(
    tmp_path: Path,
) -> None:
    source = tmp_path / "work" / "raw.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-content")
    output = tmp_path / "out.pdf"
    output.touch()  # the CLI's empty reservation

    publish_pdf(source, output)

    assert output.read_bytes() == b"%PDF-content"
    assert not source.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.pdf", "work"]


def test_publish_pdf_failure_raises_processing_error(tmp_path: Path) -> None:
    source = tmp_path / "raw.pdf"
    source.write_bytes(b"%PDF-content")

    with pytest.raises(ProcessingError, match="cannot write output"):
        publish_pdf(source, tmp_path / "missing-dir" / "out.pdf")


def test_blank_threshold_zero_disables_blank_detection(tmp_path: Path) -> None:
    page = _white_page(tmp_path / "white.pgm")
    config = dataclasses.replace(
        _config((page,), tmp_path / "out.pdf"), blank_threshold=0.0
    )

    keep, blank = analyze_page(page, 1, config, EventWriter(enabled=False))

    assert keep is True
    assert blank is False
