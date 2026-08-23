"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
from pathlib import Path

import pytest
from support.pipeline import (
    _NEEDS_IMG2PDF,
    _auto_config,
    _config,
    _gray_page,
    _run_capture,
)

from scanmole.config import ScanConfig
from scanmole.errors import (
    NoPagesError,
)
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.pipeline import analyze_page, publish_pdf, run_pipeline
from scanmole.scanner import (
    EffectiveSettings,
    ScanResult,
)

pytestmark = pytest.mark.integration


def _white_page(path: Path) -> Path:
    path.write_bytes(b"P5\n40 40\n255\n" + bytes([255] * 1600))
    return path


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
    assert "protocol" not in start  # versioning lives in the CLI's hello event
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


def test_from_images_passes_the_requested_dpi_to_build_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rebuilt inputs (scanned PNMs) carry no resolution metadata, so the
    # requested -r stamps the whole batch; without it img2pdf assumes
    # 96 dpi and every page changes size.
    page = _gray_page(tmp_path / "input.pgm")
    stamped: list[object] = []

    def fake_build_pdf(pages: object, output: Path, dpi: object) -> None:
        stamped.append(dpi)
        output.write_bytes(b"%PDF-fake")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", fake_build_pdf)

    result = run_pipeline(
        _config((page,), tmp_path / "out.pdf"), EventWriter(enabled=False)
    )

    assert result == 0
    assert stamped == [300]  # the _config resolution, applied uniformly


@_NEEDS_IMG2PDF
def test_from_images_pdf_geometry_honors_the_requested_dpi(tmp_path: Path) -> None:
    # A 300x300 px page rebuilt at -r 300 is exactly one inch square:
    # 72x72 PDF points, not the ~225 points of a 96 dpi assumption.
    page = tmp_path / "square.pgm"
    page.write_bytes(b"P5\n300 300\n255\n" + bytes([120] * 90000))
    output = tmp_path / "out.pdf"

    assert run_pipeline(_config((page,), output), EventWriter(enabled=False)) == 0

    box = re.search(rb"/MediaBox\s*\[([^\]]+)\]", output.read_bytes())
    assert box is not None
    dims = [float(value) for value in box.group(1).split()]
    assert dims[2] == pytest.approx(72, abs=0.5)
    assert dims[3] == pytest.approx(72, abs=0.5)


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
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
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


def test_from_images_are_never_binarized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _config requests lineart, but user-supplied images must stay untouched.
    original = b"P5\n4 4\n255\n" + bytes([120] * 16)
    page = tmp_path / "input.pgm"
    page.write_bytes(original)
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    config = _config((page,), tmp_path / "out.pdf")

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    assert page.read_bytes() == original


def test_blank_threshold_zero_disables_blank_detection(tmp_path: Path) -> None:
    page = _white_page(tmp_path / "white.pgm")
    config = dataclasses.replace(
        _config((page,), tmp_path / "out.pdf"), blank_threshold=0.0
    )

    keep, blank = analyze_page(page, 1, config, EventWriter(enabled=False))

    assert keep is True
    assert blank is False


def test_keep_images_batches_never_collide(tmp_path: Path) -> None:
    # Reusing one archive directory (also concurrently, mkdir is atomic)
    # must isolate batches even when outputs in different directories share
    # a name: each batch claims its own subdirectory.
    from scanmole.pipeline import copy_kept_images

    first = _gray_page(tmp_path / "a.pnm")
    second = _gray_page(tmp_path / "b.pnm")
    archive = tmp_path / "archive"

    copy_kept_images([(1, first)], archive, "scan")
    copy_kept_images([(1, second)], archive, "scan")

    assert (archive / "scan" / "page_0001.pnm").is_file()
    assert (archive / "scan_2" / "page_0001.pnm").is_file()


def _deskew_scan(settings: EffectiveSettings):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(settings)
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    return fake_scan


def test_deskew_falls_through_to_ocr_when_the_backend_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ocr_calls: list[bool] = []

    def fake_ocr(
        source: Path, output: Path, config: ScanConfig, deskew: bool = False
    ) -> None:
        ocr_calls.append(deskew)
        output.write_bytes(b"%PDF-fake")

    settings = EffectiveSettings(
        source="ADF Duplex", mode="Gray", resolution=300, deskew_applied=False
    )
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _deskew_scan(settings))
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    monkeypatch.setattr("scanmole.pipeline.run_ocr", fake_ocr)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), deskew=True, ocr=True
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    assert ocr_calls == [True]


def test_deskew_stays_off_in_ocr_when_the_backend_took_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ocr_calls: list[bool] = []

    def fake_ocr(
        source: Path, output: Path, config: ScanConfig, deskew: bool = False
    ) -> None:
        ocr_calls.append(deskew)
        output.write_bytes(b"%PDF-fake")

    settings = EffectiveSettings(
        source="ADF Duplex", mode="Lineart", resolution=300, deskew_applied=True
    )
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _deskew_scan(settings))
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    monkeypatch.setattr("scanmole.pipeline.run_ocr", fake_ocr)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), deskew=True, ocr=True
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    assert ocr_calls == [False]  # the backend already straightened the pages


def test_deskew_dead_end_warns_instead_of_staying_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = EffectiveSettings(
        source="ADF Duplex", mode="Gray", resolution=300, deskew_applied=False
    )
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _deskew_scan(settings))
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), deskew=True, ocr=False
    )

    with caplog.at_level("WARNING"):
        assert run_pipeline(config, EventWriter(enabled=False)) == 0

    assert any("deskew requested" in record.message for record in caplog.records)


def _uniform_gray(value: int) -> bytes:
    return b"P5\n100 100\n255\n" + bytes([value] * 10000)


def test_blank_threshold_boundary_and_raising_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted limitation: sparse content near the cutoff can land on
    # either side. Gray pages with exactly known means pin both sides of
    # the default boundary, and raising the threshold is the documented
    # remedy that keeps the near-blank page.
    near_blank = _uniform_gray(254)  # mean 254/255, just above 0.995
    sparse_kept = _uniform_gray(253)  # mean 253/255, just below 0.995
    config = _auto_config(tmp_path, mode="gray", lineart_threshold=0.5)

    events = _run_capture(config, monkeypatch, [near_blank, sparse_kept])

    first, second = (e for e in events if e["event"] == "page")
    assert first["blank"] is True and second["blank"] is False
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1

    (tmp_path / "raised").mkdir()
    raised = _auto_config(
        tmp_path / "raised", mode="gray", lineart_threshold=0.5, blank_threshold=0.998
    )
    events = _run_capture(raised, monkeypatch, [near_blank, sparse_kept])

    assert all(e["blank"] is False for e in events if e["event"] == "page")
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 2 and scan_done["blanks"] == 0


def test_keep_blanks_and_zero_threshold_differ_in_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both remedies keep the page, but they mean different things:
    # --keep-blanks retains a page still reported blank, while
    # --blank-threshold 0 removes the classification itself.
    near_blank = _uniform_gray(254)

    kept_dir = tmp_path / "kept"
    kept_dir.mkdir()
    keeping = _auto_config(kept_dir, mode="gray", lineart_threshold=0.5)
    keeping = dataclasses.replace(keeping, keep_blanks=True)
    events = _run_capture(keeping, monkeypatch, [near_blank])
    page = next(e for e in events if e["event"] == "page")
    assert page["blank"] is True  # still classified, just not dropped
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1

    disabled = _auto_config(
        tmp_path, mode="gray", lineart_threshold=0.5, blank_threshold=0
    )
    events = _run_capture(disabled, monkeypatch, [near_blank])
    page = next(e for e in events if e["event"] == "page")
    assert page["blank"] is False  # never classified at all
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 0
