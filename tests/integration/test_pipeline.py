"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
import random
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import scanmole.pipeline as pipeline_module
from scanmole.config import ScanConfig
from scanmole.errors import (
    DeviceError,
    NoPagesError,
    ProcessingError,
    ScanMoleError,
)
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.pipeline import analyze_page, publish_pdf, run_pipeline
from scanmole.scanner import EffectiveSettings, ScanResult

pytestmark = pytest.mark.integration

_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)


def _gray_page(path: Path) -> Path:
    # 40x40: large enough to stay above img2pdf's 3-point minimum at 300 dpi.
    path.write_bytes(b"P5\n40 40\n255\n" + bytes([120] * 1600))
    return path


def _white_page(path: Path) -> Path:
    path.write_bytes(b"P5\n40 40\n255\n" + bytes([255] * 1600))
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


def test_processing_failure_preserves_scanned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
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
        on_settings: object = None,
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


def _unannounced_scan(pages: list[bytes], error: BaseException):  # type: ignore[no-untyped-def]
    """A scan that writes page files without announcing them, then dies."""

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        for index, data in enumerate(pages, start=1):
            (work_dir / f"page_{index:04d}.pnm").write_bytes(data)
        raise error

    return fake_scan


def _owned_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work_dir = tmp_path / "preserved-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)
    return work_dir


_COMPLETE_FRAME = b"P5\n40 40\n255\n" + bytes([120] * 1600)


def test_unannounced_complete_frame_survives_a_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scanner finished writing the frame but died before --batch-print
    # announced it: no callback ran, yet the file may be the only copy.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME], ScanMoleError("lamp failure")),
    )
    stream = io.StringIO()

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=True, stream=stream),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME
    assert info.value.message.startswith("lamp failure")  # original cause
    assert "incomplete" in info.value.message  # the inspect caveat
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert not [e for e in events if e["event"] == "page"]  # never announced


def test_unannounced_frame_survives_an_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME], KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME


@pytest.mark.parametrize(
    "tail",
    [b"P5\n40 40\n255\n" + bytes([120] * 100), b""],
    ids=["partial", "zero-length"],
)
def test_partial_final_frame_is_preserved_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: bytes
) -> None:
    # Questionable bytes are evidence: preserved exactly, never validated,
    # renamed or fed to processing during failure handling.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME, tail], ScanMoleError("feeder jam")),
    )

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME
    assert (work_dir / "page_0002.pnm").read_bytes() == tail
    assert info.value.message.startswith("feeder jam")


def test_no_artifacts_and_no_callbacks_removes_the_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([], ScanMoleError("no device")),
    )

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert not work_dir.exists()  # nothing to keep: no litter either
    assert "preserved" not in info.value.message


def test_announced_and_unannounced_pages_keep_both_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One page went through its callback, a second was still unannounced:
    # the established recovery message stays, plus the inspect caveat.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir_arg: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        page = work_dir_arg / "page_0001.pnm"
        page.write_bytes(_COMPLETE_FRAME)
        assert callable(on_page)
        on_page(page)
        (work_dir_arg / "page_0002.pnm").write_bytes(_COMPLETE_FRAME[:20])
        raise ScanMoleError("feeder jam")

    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert f"the 1 scanned page(s) are kept in {work_dir}" in info.value.message
    assert "recover with:" in info.value.message
    assert "incomplete" in info.value.message
    assert (work_dir / "page_0002.pnm").read_bytes() == _COMPLETE_FRAME[:20]


def test_from_images_failure_never_preserves_a_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # User inputs are not scanner output: a failing run neither claims
    # nor keeps copies of them.
    image = _gray_page(tmp_path / "in.pgm")
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def failing_build(pages: object, output: Path, dpi: int) -> None:
        raise ScanMoleError("assembly failed")

    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=(image,), output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert not work_dir.exists()
    assert "kept" not in info.value.message
    assert "preserved" not in info.value.message
    assert image.is_file()  # the input itself is untouched


def _failing_scan_at(resolution: int):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=resolution))
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise ScanMoleError("feeder jam")

    return fake_scan


def test_recovery_command_names_the_snapped_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rebuild must use the dpi the pages were actually scanned at
    # (after snapping), not the requested value: 300 snapped to 150 here.
    work_dir = tmp_path / "preserved-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _failing_scan_at(150))
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (
        f"--from-images {work_dir}/page_*.pnm -r 150 -o out.pdf" in info.value.message
    )


def test_recovery_command_quotes_shell_sensitive_work_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The message is meant to be pasted into a shell: a work dir with
    # spaces and quotes must come out quoted, with the glob outside the
    # quotes so it still expands.
    work_dir = tmp_path / "scan mole's work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _failing_scan_at(300))
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    quoted = shlex.quote(str(work_dir))
    assert quoted.startswith("'")  # the path genuinely needed quoting
    assert f"--from-images {quoted}/page_*.pnm -r 300 -o out.pdf" in info.value.message


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


def _fake_gray_scan(
    config: ScanConfig,
    device: str,
    work_dir: Path,
    events: EventWriter,
    on_page: object,
    on_settings: object = None,
) -> ScanResult:
    # Emulates a backend without a 1-bit mode: it delivers a gray page even
    # though lineart was requested (dark text pixels on a bright background).
    page = work_dir / "page_0001.pnm"
    page.write_bytes(b"P5\n4 4\n255\n" + bytes([20] * 8 + [250] * 8))
    assert callable(on_page)
    on_page(page)
    return ScanResult(
        pages=[page],
        settings=EffectiveSettings(source=None, mode="Gray", resolution=300),
    )


def test_lineart_request_binarizes_gray_scanner_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _fake_gray_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), keep_images=keep_dir
    )
    stream = io.StringIO()

    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0

    kept = keep_dir / "out" / "page_0001.pnm"
    assert kept.read_bytes().startswith(b"P4\n")
    page_event = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "page"
    )
    assert page_event["mean"] == pytest.approx(0.5)  # measured after conversion


def test_lineart_threshold_zero_keeps_the_gray_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _fake_gray_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        keep_images=keep_dir,
        lineart_threshold=0.0,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    assert (keep_dir / "out" / "page_0001.pnm").read_bytes().startswith(b"P5\n")


def test_auto_page_size_crops_before_binarization_and_blank_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blank backside surrounded by dark backing: without the crop the
    # backing pixels binarize to black and rescue the page from the blank
    # drop; with page size auto the page must come out blank and dropped.
    def bordered_page(*, with_ink: bool) -> bytes:
        # 120x90 frame, paper spans columns 30-89 and rows 10-79; the content
        # page carries a dark ink band inside the paper area.
        rows = []
        for y in range(90):
            if 10 <= y <= 79:
                paper = 30 if with_ink and 40 <= y <= 45 else 250
                rows.append(bytes([110] * 30 + [paper] * 60 + [110] * 30))
            else:
                rows.append(bytes([110] * 120))
        return b"P5\n120 90\n255\n" + b"".join(rows)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        content = work_dir / "page_0001.pnm"
        content.write_bytes(bordered_page(with_ink=True))
        blank = work_dir / "page_0002.pnm"
        blank.write_bytes(bordered_page(with_ink=False))
        assert callable(on_page)
        on_page(content)
        on_page(blank)
        return ScanResult(
            pages=[content, blank],
            settings=EffectiveSettings(source=None, mode="Gray", resolution=300),
        )

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        keep_images=keep_dir,
    )
    stream = io.StringIO()

    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    scan_done = next(event for event in events if event["event"] == "scan_done")
    assert scan_done == {"event": "scan_done", "total": 2, "kept": 1, "blanks": 1}
    kept_page = keep_dir / "out" / "page_0001.pnm"
    header = kept_page.read_bytes().split(b"\n", 2)
    assert header[0] == b"P4"  # cropped, then binarized
    width, height = map(int, header[1].split())
    assert width < 120 and height < 90  # backing removed


def test_auto_page_size_sizes_white_backed_frames_by_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # White backing: full-window frames with no detectable paper edge. The
    # batch must come out at the majority standard size (A4 here), and the
    # sparse second page must survive blank detection. (For the boundary
    # case where only the content-box mean keeps a sparse page, see
    # test_sparse_full_window_page_survives_via_the_content_box_mean.)
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)

    def white_frame(boxes: list[tuple[int, int, int, int]]) -> bytes:
        row_bytes = (frame_w + 7) // 8
        raster = bytearray(row_bytes * frame_h)
        for x0, y0, x1, y1 in boxes:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        return b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        dense = work_dir / "page_0001.pnm"
        dense.write_bytes(
            white_frame(
                [(round(20 * scale), 0, round(190 * scale), round(270 * scale))]
            )
        )
        sparse = work_dir / "page_0002.pnm"
        sparse.write_bytes(
            white_frame(
                [
                    (
                        round(30 * scale),
                        round(40 * scale),
                        round(120 * scale),
                        round(60 * scale),
                    )
                ]
            )
        )
        assert callable(on_page)
        on_page(dense)
        on_page(sparse)
        return ScanResult(pages=[dense, sparse], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    for name in ("page_0001.pnm", "page_0002.pnm"):
        header = (keep_dir / "out" / name).read_bytes().split(b"\n", 2)
        width, height = map(int, header[1].split())
        # Both kept and both exactly A4 (byte-grid alignment may add <8 px).
        assert abs(width - round(210 * scale)) < 8
        assert height == round(297 * scale)


def test_mid_batch_failure_preserves_sized_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented recovery command uses --from-images, which never crops;
    # pages preserved by a failed run must therefore be sized before the
    # error propagates, or recovery resurrects the full-window frames.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        row_bytes = (frame_w + 7) // 8
        raster = bytearray(row_bytes * frame_h)
        for y in range(0, round(270 * scale)):
            for index in range(10, 90):
                raster[y * row_bytes + index] = 0xFF
        page = work_dir / "page_0001.pnm"
        page.write_bytes(b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster))
        assert callable(on_page)
        on_page(page)
        raise DeviceError("scanner unplugged mid-batch")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), page_size="auto"
    )

    with pytest.raises(DeviceError) as info:
        run_pipeline(config, EventWriter(enabled=False))

    work_dir = Path(info.value.message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        header = (work_dir / "page_0001.pnm").read_bytes().split(b"\n", 2)
        _width, height = map(int, header[1].split())
        assert height == round(297 * scale)  # sized to A4, not the window
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_recovery_sizing_waits_for_the_active_page_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupt mid-batch must not start recovery sizing (or escape to
    # the caller) while a page is still being analyzed: the scanner drains
    # its callbacks first, so the page event always precedes both.
    import threading

    from scanmole.pnm import pnm_mean
    from scanmole.scanner import EffectiveSettings

    order: list[str] = []
    entered = threading.Event()
    work_dirs: list[Path] = []

    def fake_build(
        config: ScanConfig,
        device: str,
        caps: object,
        pattern: str,
        plan: object = None,
    ) -> tuple[list[str], EffectiveSettings]:
        page = pattern.replace("%04d", "0001")
        work_dirs.append(Path(page).parent)
        script = (
            f"printf 'P5\\n4 4\\n255\\n0123456789abcdef' > '{page}'; "
            f"echo '{page}'; exec sleep 30"
        )
        return ["sh", "-c", script], EffectiveSettings(
            source=None, mode=None, resolution=75
        )

    def blocking_mean(page: Path) -> float | None:
        if not entered.is_set():
            entered.set()
            release = threading.Event()
            threading.Timer(0.25, release.set).start()
            release.wait(10)
            order.append("callback-finished")
        return pnm_mean(page)

    real_size = pipeline_module._size_preserved_pages

    def recording_size(*args: object, **kwargs: object) -> None:
        order.append("recovery-sizing")
        real_size(*args, **kwargs)  # type: ignore[arg-type]

    real_wait = subprocess.Popen.wait
    armed = {"value": True}

    def interrupting_wait(
        self: subprocess.Popen[str], timeout: float | None = None
    ) -> int:
        if armed["value"]:
            armed["value"] = False
            assert entered.wait(10)
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr("scanmole.scanner.build_scan_command", fake_build)
    monkeypatch.setattr("scanmole.pipeline.image_mean", blocking_mean)
    monkeypatch.setattr("scanmole.pipeline._size_preserved_pages", recording_size)
    monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)

    stream = io.StringIO()
    config = _config(images=None, output=tmp_path / "out.pdf")
    try:
        with pytest.raises(KeyboardInterrupt):
            run_pipeline(config, EventWriter(enabled=True, stream=stream))
        order.append("raised")

        # The callback finished first, then recovery sizing, then the
        # terminal raise; the page event is on the stream by then.
        assert order == ["callback-finished", "recovery-sizing", "raised"]
        kinds = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
        assert "page" in kinds  # emitted during the drain, before the raise
    finally:
        for work_dir in work_dirs:
            shutil.rmtree(work_dir, ignore_errors=True)


def test_keyboard_interrupt_preserves_scanned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C mid-batch must not delete the only copy of already-fed paper.
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise KeyboardInterrupt

    # Own the work directory instead of diffing a global /tmp glob, which
    # could sweep up (and delete) directories of concurrent suites or of a
    # real scan running on this machine.
    work_dir = tmp_path / "scanmole-interrupt-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config, EventWriter(enabled=False))

    assert (work_dir / "page_0001.pnm").is_file()  # preserved for recovery


def test_hardware_cropped_frames_stay_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A frame shortened on BOTH axes is the device's own complete result;
    # content sizing must not touch it. (One shortened axis resolves only
    # that axis: see the partial-crop test below.)
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(210 * scale), round(215 * scale)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        row_bytes = (frame_w + 7) // 8
        raster = bytearray(row_bytes * frame_h)
        for y in range(40, 700):  # a dense content block, clearly not blank
            for index in range(10, 60):
                raster[y * row_bytes + index] = 0xFF
        page = work_dir / "page_0001.pnm"
        page.write_bytes(b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster))
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    header = (keep_dir / "out" / "page_0001.pnm").read_bytes().split(b"\n", 2)
    width, height = map(int, header[1].split())
    assert (width, height) == (frame_w, frame_h)  # exactly as delivered


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


def _p4_window_frame(
    frame_w: int, frame_h: int, boxes: list[tuple[int, int, int, int]]
) -> bytes:
    row_bytes = (frame_w + 7) // 8
    raster = bytearray(row_bytes * frame_h)
    for x0, y0, x1, y1 in boxes:
        for y in range(y0, y1):
            for x in range(x0, x1):
                raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    return b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster)


def _partial_crop_scan(frame_w: int, frame_h: int, boxes, dpi: int, window):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(_p4_window_frame(frame_w, frame_h, boxes))
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    return fake_scan


def test_partially_cropped_frame_gets_the_window_axis_sized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan_006 field case: hardware shortened the height (~302 mm) but
    # left the width at the scan window. The width must be sized instead of
    # the frame being bypassed as "hardware handled it".
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(215.3 * scale), round(302.6 * scale)
    content = (round(10 * scale), 0, round(205 * scale), round(270 * scale))

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _partial_crop_scan(frame_w, frame_h, [content], dpi, window),
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    header = (keep_dir / "out" / "page_0001.pnm").read_bytes().split(b"\n", 2)
    width, height = map(int, header[1].split())
    assert abs(width - round(210 * scale)) < 8  # width sized to A4
    assert height == round(297 * scale)  # observed height snapped to A4


def test_one_axis_brightness_crop_does_not_suppress_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A frame already narrow (as after a side-only brightness crop) but still
    # at window height: the height axis is unresolved and must be sized.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(180 * scale), round(393.5 * scale)
    content = (round(10 * scale), 0, round(170 * scale), round(200 * scale))

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _partial_crop_scan(frame_w, frame_h, [content], dpi, window),
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    header = (keep_dir / "out" / "page_0001.pnm").read_bytes().split(b"\n", 2)
    width, height = map(int, header[1].split())
    assert width == frame_w  # observed width preserved whole
    assert height < round(393.5 * scale)  # window height content-cropped


def _autocrop_probe(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requested_dpi: int,
    effective_dpi: int | None,
    effective_source: str | None,
    config_source: str = "adf",
):
    """Run one fake scan and record the (trim, band) autocrop received."""
    calls: list[tuple[int, int | None]] = []

    def recorder(page: Path, trim_px: int, feeder_band_px: int | None = None) -> bool:
        calls.append((trim_px, feeder_band_px))
        return False

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source=effective_source, mode="Gray", resolution=effective_dpi
        )
        assert callable(on_settings)
        on_settings(settings)
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr("scanmole.pipeline.autocrop_image", recorder)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source=config_source,
        resolution=requested_dpi,
    )
    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    assert len(calls) == 1
    return calls[0]


def test_trim_and_band_derive_from_the_effective_dpi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 600 dpi requested, snapped to 150: the ~1/3 mm trim is 2 px at the
    # dpi the frame actually has, not the 8 px the request implies.
    trim, band = _autocrop_probe(
        tmp_path,
        monkeypatch,
        requested_dpi=600,
        effective_dpi=150,
        effective_source="ADF Front",
    )

    assert trim == 2
    assert band == round(50 * 150 / 25.4)


def test_trim_matches_when_the_resolution_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trim, band = _autocrop_probe(
        tmp_path,
        monkeypatch,
        requested_dpi=300,
        effective_dpi=300,
        effective_source="ADF Duplex",
    )

    assert trim == 4
    assert band == round(50 * 300 / 25.4)


@pytest.mark.parametrize(
    ("effective_source", "config_source", "expect_band"),
    [
        ("ADF Front", "adf", True),  # positively mapped feeder
        ("ADF Duplex", "adf-duplex", True),
        ("Flatbed", "adf", False),  # request degraded to a mapped flatbed
        ("Document Table", "flatbed", False),
        (None, "adf", False),  # UNKNOWN source: requesting adf proves nothing
        (None, "flatbed", False),
    ],
)
def test_feeder_band_requires_a_positively_mapped_feeder_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effective_source: str | None,
    config_source: str,
    expect_band: bool,
) -> None:
    _trim, band = _autocrop_probe(
        tmp_path,
        monkeypatch,
        requested_dpi=300,
        effective_dpi=300,
        effective_source=effective_source,
        config_source=config_source,
    )

    assert (band is not None) is expect_band


def test_snapped_dpi_trim_keeps_near_edge_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Behavioral proof for the trim fix: content 3 px inside the detected
    # paper edge of a 150 dpi frame survives (2 px trim); the request-derived
    # 8 px trim would have deleted it.
    dpi = 150
    scale = dpi / 25.4
    window = (215.9, 355.6)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    backing = 24  # ~4 mm per side: the cropped width resolves conclusively
    paper_end = round(297 * scale)
    rows = []
    for y in range(frame_h):
        if y < paper_end:
            row = bytearray(
                [80] * backing + [230] * (frame_w - 2 * backing) + [80] * backing
            )
            if 200 <= y < 800:
                row[backing + 3 : backing + 6] = bytes(3)  # near-edge strip
            if 100 <= y < 1600:
                row[300:900] = bytes(600)  # dense block: keeps the page
            rows.append(bytes(row))
        else:
            rows.append(bytes([80] * frame_w))
    frame = b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Front", mode="Gray", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source="adf",
        resolution=600,  # requested; the device snapped to 150
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    _magic, dims, raster = kept.split(b"\n", 2)
    width, _height = map(int, dims.split())
    assert width == frame_w - 2 * backing - 4  # 2 px trim per side, not 8
    row_bytes = (width + 7) // 8
    # The strip sat 3 px inside the paper edge; after the 2 px trim it is
    # bit 1 of each row's first byte on its rows.
    assert any(raster[row * row_bytes] != 0 for row in range(200, 780))


def test_huge_feeder_window_with_mid_gray_tail_yields_a4_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ADS-4550W simplex case: the device delivers the full multi-metre
    # window, padding everything below the paper with uniform mid-gray.
    # That tail dilutes every full-height column mean below the paper
    # cutoff, so the ordinary crop found nothing, the dark side backing
    # binarized into full-height black bars, and sizing shipped Legal.
    # The feeder leading-edge fallback must recover A4 without bars.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 1016.0)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    backing_px = 12
    paper_rows = round(297 * scale)
    paper_row = bytearray([80] * frame_w)
    for column in range(backing_px, frame_w - backing_px):
        paper_row[column] = 230
    rows = []
    for y in range(frame_h):
        if y < paper_rows:
            row = bytearray(paper_row)
            if 80 <= y < 1063:  # dense content block
                row[80:760] = bytes([0] * 680)
            rows.append(bytes(row))
        else:
            rows.append(bytes([128] * frame_w))  # synthetic mid-gray tail
    frame = b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Front", mode="Gray", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source="adf",
        resolution=dpi,
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    header = kept.split(b"\n", 3)
    width, height = map(int, header[1].split())
    assert width == frame_w - 2 * backing_px - 2  # side backing gone, no bars
    assert abs(height - paper_rows) <= 2  # resolved at the paper end
    raster = kept.split(b"\n", 2)[2]
    row_bytes = (width + 7) // 8
    left_band = bytes(raster[row * row_bytes] for row in range(0, height, 50))
    assert set(left_band) == {0}  # the left margin is white, not a black bar


def test_white_clipped_height_is_content_sized_not_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An A4 sheet whose lower margin the device white-clipped to 255: the
    # bright rows are indistinguishable from end-of-paper padding, so the
    # brightness crop resolves only the width (dark side backing). The
    # height must stay at the scan window and be content-sized to the
    # standard 297 mm, not stripped to a Letter-like 279 mm by a padding
    # heuristic.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    backing_px = 12  # ~3 mm of dark backing on each side
    paper_row = (
        bytes([80] * backing_px)
        + bytes([230] * (frame_w - 2 * backing_px))
        + bytes([80] * backing_px)
    )
    white_row = bytes([255] * frame_w)
    rows = []
    for y in range(frame_h):
        if y < 1100:  # paper, white-clipped from ~279 mm downward
            row = bytearray(paper_row)
            if 80 <= y < 1063:  # dense content down to ~270 mm
                row[80:760] = bytes([0] * 680)
            rows.append(bytes(row))
        else:
            rows.append(white_row)
    frame = b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        resolution=dpi,
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    header = (keep_dir / "out" / "page_0001.pnm").read_bytes().split(b"\n", 2)
    width, height = map(int, header[1].split())
    assert width == frame_w - 2 * backing_px - 2  # side crop plus trim only
    assert height == round(297 * scale)  # unresolved height snapped to A4


def _gray_scan_pages(specs: list[bytes], faint_native: bool = False):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex",
            mode="Gray",
            resolution=300,
            faint_native=faint_native,
        )
        assert callable(on_settings)
        on_settings(settings)
        pages = []
        for index, data in enumerate(specs, start=1):
            page = work_dir / f"page_{index:04d}.pnm"
            page.write_bytes(data)
            assert callable(on_page)
            on_page(page)
            pages.append(page)
        return ScanResult(pages=pages, settings=settings)

    return fake_scan


def _faint_page() -> bytes:
    # Faint-only strokes at 170 on 235 paper: invisible to the fixed cut.
    return b"P5\n100 100\n255\n" + bytes([170] * 800 + [235] * 9200)


def _dark_page() -> bytes:
    return b"P5\n100 100\n255\n" + bytes([40] * 1500 + [235] * 8500)


def _true_blank() -> bytes:
    return b"P5\n100 100\n255\n" + bytes([240] * 10000)


def _auto_config(tmp_path: Path, **overrides: object) -> ScanConfig:
    base = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        lineart_threshold="auto",
        page_size="a4",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _run_capture(
    config: ScanConfig,
    monkeypatch: pytest.MonkeyPatch,
    specs: list[bytes],
    faint_native: bool = False,
) -> list[dict[str, object]]:
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files", _gray_scan_pages(specs, faint_native)
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    stream = io.StringIO()
    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_auto_threshold_thin_faint_band_still_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 8-row faint band is a thin-band negative for the coherent rescue:
    # its ink forms a single tile row, so the page stays a fixed-0.5 blank
    # and is dropped, exactly as with a numeric threshold.
    events = _run_capture(
        _auto_config(tmp_path), monkeypatch, [_dark_page(), _faint_page()]
    )

    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1


def test_auto_threshold_keep_blanks_recovers_a_faint_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_blanks=True, keep_images=keep_dir)

    events = _run_capture(config, monkeypatch, [_faint_page()])

    page_event = next(e for e in events if e["event"] == "page")
    assert page_event["blank"] is True  # verdict metric stays fixed-0.5
    mean_value = page_event["mean"]
    assert isinstance(mean_value, float) and mean_value > 0.99
    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    assert kept.startswith(b"P4")
    from scanmole.pnm import pnm_mean

    mean = pnm_mean(keep_dir / "out" / "page_0001.pnm")
    assert mean is not None and mean < 0.95  # faint strokes recovered


def test_auto_threshold_true_blank_stays_clean_with_keep_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_blanks=True, keep_images=keep_dir)

    _run_capture(config, monkeypatch, [_true_blank()])

    from scanmole.pnm import pnm_mean

    mean = pnm_mean(keep_dir / "out" / "page_0001.pnm")
    assert mean == pytest.approx(1.0)  # guards fell back to the fixed result


def test_auto_and_fixed_emit_identical_page_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [_dark_page(), _true_blank()]
    (tmp_path / "fixed").mkdir()
    auto_events = _run_capture(_auto_config(tmp_path), monkeypatch, list(specs))
    fixed_events = _run_capture(
        _auto_config(tmp_path / "fixed", lineart_threshold=0.5),
        monkeypatch,
        list(specs),
    )

    def pages(events: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {k: v for k, v in e.items() if k in ("event", "n", "blank", "mean")}
            for e in events
            if e["event"] == "page"
        ]

    assert pages(auto_events) == pages(fixed_events)


def _faint_text_page(width: int = 600, height: int = 400) -> bytes:
    """Wholly faint text: dashed lines at 170 on noisy 235 paper.

    Every stroke sits above the fixed 0.5 cut, so the fixed conversion
    yields an all-white P4; only the coherent rescue can keep this page.
    """
    raster = bytearray(234 + (x + y) % 3 for y in range(height) for x in range(width))
    for y0 in (100, 160, 220):
        for x0 in range(60, 504, 60):
            for y in range(y0, y0 + 24):
                raster[y * width + x0 : y * width + x0 + 36] = b"\xaa" * 36
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)


def _pepper_page(width: int = 1000, height: int = 1400) -> bytes:
    """1% random pixels at 170 on 235 paper: Otsu accepts, coherence must not."""
    rng = random.Random(42)
    raster = bytearray([235]) * (width * height)
    for _ in range(width * height // 100):
        raster[rng.randrange(width * height)] = 170
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)


def test_auto_threshold_rescues_a_wholly_faint_text_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_images=keep_dir)

    events = _run_capture(config, monkeypatch, [_faint_text_page()])

    page_event = next(e for e in events if e["event"] == "page")
    assert page_event["blank"] is False
    mean_value = page_event["mean"]
    # The reported mean is the coherent region's adaptive mean: it explains
    # why the page is nonblank instead of claiming an all-white 1.0.
    assert isinstance(mean_value, float) and 0.5 < mean_value < 0.9
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 0
    kept = keep_dir / "out" / "page_0001.pnm"
    assert kept.read_bytes().startswith(b"P4")
    from scanmole.pnm import pnm_mean

    mean = pnm_mean(kept)
    assert mean is not None and mean < 0.95  # the recovered strokes are real ink


def test_pepper_noise_is_never_rescued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mandatory false-positive regression end-to-end: Otsu accepts the
    # bimodal split, the projection bbox spans the frame, but the candidate
    # holds no coherent region, so the page stays a dropped blank.
    events = _run_capture(
        _auto_config(tmp_path), monkeypatch, [_dark_page(), _pepper_page()]
    )

    second = [e for e in events if e["event"] == "page"][1]
    assert second["blank"] is True
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1


def test_rescue_respects_a_custom_blank_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The coherent region mean (~0.7) must pass the *configured* threshold;
    # at 0.5 the rescue is refused and the page stays dropped.
    heavy = b"P5\n100 100\n255\n" + bytes([40] * 6000 + [235] * 4000)
    config = _auto_config(tmp_path, blank_threshold=0.5)

    events = _run_capture(config, monkeypatch, [heavy, _faint_text_page()])

    second = [e for e in events if e["event"] == "page"][1]
    assert second["blank"] is True
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1


def test_failed_adoption_never_rescues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Coherence evidence alone must not flip the verdict: if the atomic
    # adoption fails, the fixed all-white page stands and stays dropped.
    monkeypatch.setattr(
        "scanmole.pipeline._adopt_candidate", lambda staging, page: False
    )

    events = _run_capture(
        _auto_config(tmp_path), monkeypatch, [_dark_page(), _faint_text_page()]
    )

    second = [e for e in events if e["event"] == "page"][1]
    assert second["blank"] is True
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1


def test_failed_candidate_staging_never_rescues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.pipeline._stage_adaptive", lambda page, snapshot, fraction: None
    )

    events = _run_capture(
        _auto_config(tmp_path), monkeypatch, [_dark_page(), _faint_text_page()]
    )

    second = [e for e in events if e["event"] == "page"][1]
    assert second["blank"] is True


def _gray_window_scan(page_bytes: bytes, dpi: int, window):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Front", mode="Gray", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(page_bytes)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    return fake_scan


def test_rescued_page_is_sized_from_the_coherent_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full-window white-backed frame whose only content is wholly faint:
    # the fixed measurement sees a blank, so the coherent box must become
    # the robust sizing evidence. Content demanding ~138 x 83 mm from the
    # leading edge snaps to A6 landscape (148 x 105 mm), the smallest
    # standard cover, instead of keeping the full A4 window.
    dpi = 75
    scale = dpi / 25.4
    width, height = round(210 * scale), round(297 * scale)
    raster = bytearray(234 + (x + y) % 3 for y in range(height) for x in range(width))
    for y0 in range(30, 260, 40):
        for x0 in range(30, 410, 40):
            for y in range(y0, y0 + 12):
                raster[y * width + x0 : y * width + x0 + 24] = b"\xaa" * 24
    frame = b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)

    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, page_size="auto", keep_images=keep_dir)
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _gray_window_scan(frame, dpi, (210.0, 297.0)),
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    assert kept.startswith(b"P4")
    kept_w, kept_h = (int(v) for v in kept.split(b"\n")[1].split(b" "))
    assert kept_w < width and kept_h < height  # the window was not kept
    assert abs(kept_w - round(148 * scale)) <= 8  # A6 landscape (byte-aligned)
    assert abs(kept_h - round(105 * scale)) <= 2


def test_auto_threshold_keeps_natively_enhanced_p4_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On the native text-enhancement path 1-bit frames are the intended
    # enhanced result and pass through unmodified.
    native = b"P4\n16 16\n" + bytes([0xF0] * 2 * 16)
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_images=keep_dir)

    _run_capture(config, monkeypatch, [native], faint_native=True)

    assert (keep_dir / "out" / "page_0001.pnm").read_bytes() == native


def test_auto_threshold_stops_on_an_unenhanced_p4_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unknown capabilities allow a best-effort scan, but a plain 1-bit frame
    # cannot satisfy the faint request: the run must fail and preserve the
    # acquired page instead of silently succeeding.
    native = b"P4\n16 16\n" + bytes([0xF0] * 2 * 16)
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _gray_scan_pages([native]))

    with pytest.raises(ProcessingError, match="cannot preserve faint") as excinfo:
        run_pipeline(_auto_config(tmp_path), EventWriter(enabled=False))

    match = re.search(r"kept in (\S+)", str(excinfo.value))
    assert match is not None
    preserved = Path(match.group(1))
    assert (preserved / "page_0001.pnm").read_bytes() == native
    shutil.rmtree(preserved, ignore_errors=True)


def test_auto_threshold_adaptive_reach_protects_recovered_strokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Full-window gray frame: dark block chooses the size (fixed-0.5 bbox),
    # a faint stroke row further down is invisible to the fixed pass but
    # must survive the crop via the adaptive reach union.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    faint_row = round(320 * scale)
    dark = (
        round(30 * scale),
        round(170 * scale),
        round(40 * scale),
        round(120 * scale),
    )
    faint = (round(20 * scale), round(170 * scale), faint_row, round(354 * scale))
    rows = []
    for y in range(frame_h):
        # Slight background noise: real sensor data is never bit-uniform,
        # and a perfectly flat background would trip the synthetic-padding
        # stripper of the edge walk.
        row = bytearray(234 + (x + y) % 3 for x in range(frame_w))
        if dark[2] <= y < dark[3]:
            row[dark[0] : dark[1]] = bytes([110]) * (dark[1] - dark[0])
        if faint[2] <= y < faint[3]:
            row[faint[0] : faint[1]] = bytes([170]) * (faint[1] - faint[0])
        rows.append(bytes(row))
    frame = b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex", mode="Gray", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=settings)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, page_size="auto", keep_images=keep_dir)

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    header = (keep_dir / "out" / "page_0001.pnm").read_bytes().split(b"\n", 2)
    width, height = map(int, header[1].split())
    assert height >= faint_row + 8  # recovered strokes inside the crop
    assert width < frame_w  # the width was still sized (fixed bbox decided)


def test_cli_accepts_auto_and_rejects_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scanmole.cli import _build_config, build_parser

    # Config building resolves the scanner once; this test is about
    # argument validation, not discovery.
    monkeypatch.setattr("scanmole.cli.pick_default_device", lambda: "stub:0")
    parser = build_parser()
    auto = parser.parse_args(
        ["--lineart-threshold", "auto", "-o", str(tmp_path / "a.pdf")]
    )
    assert _build_config(auto).lineart_threshold == "auto"

    bad = parser.parse_args(
        ["--lineart-threshold", "1.2", "-o", str(tmp_path / "a.pdf")]
    )
    with pytest.raises(Exception, match="lineart-threshold"):
        _build_config(bad)

    with pytest.raises(SystemExit):
        parser.parse_args(["--lineart-threshold", "abc", "-o", str(tmp_path / "a.pdf")])


# ------------------------------------- accepted detection-limit policies


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


def test_ordinary_lineart_never_reaches_the_faint_rescue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The coherence rescue exists only behind --lineart-threshold auto: the
    # same wholly faint page the auto mode rescues stays a dropped blank
    # under a numeric threshold.
    config = _auto_config(tmp_path, lineart_threshold=0.5)

    events = _run_capture(config, monkeypatch, [_dark_page(), _faint_text_page()])

    second = [e for e in events if e["event"] == "page"][1]
    assert second["blank"] is True
    scan_done = next(e for e in events if e["event"] == "scan_done")
    assert scan_done["kept"] == 1 and scan_done["blanks"] == 1


def _mixed_dark_faint_page(width: int = 600, height: int = 400) -> bytes:
    """Ordinary dark print plus a separate, much fainter region."""
    raster = bytearray([235]) * (width * height)
    for y in range(40, 80):
        for x in range(210, 390):
            raster[y * width + x] = 30
    for y in range(150, 350):
        for x in range(60, 540):
            raster[y * width + x] = 200
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)


def test_faint_mode_adapts_one_global_cut_per_mixed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted limitation of the guarded global threshold: on a page
    # mixing normal print with a much fainter region, the single cut keeps
    # the dark print and loses the faint region (here the guards reject
    # the split and the fixed result stands).
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_images=keep_dir)

    events = _run_capture(config, monkeypatch, [_mixed_dark_faint_page()])

    page = next(e for e in events if e["event"] == "page")
    assert page["blank"] is False  # the dark print keeps the page
    kept = keep_dir / "out" / "page_0001.pnm"
    assert kept.read_bytes().startswith(b"P4")
    from scanmole.pnm import pnm_mean

    mean = pnm_mean(kept)
    assert mean is not None
    assert mean < 0.99  # the dark print survived as ink
    assert mean > 0.9  # the 40% faint region did not: it binarized white


def test_gray_mode_retains_both_intensity_populations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented escape hatch for mixed-intensity originals: Gray does
    # not binarize, so both the dark print and the faint region survive.
    keep_dir = tmp_path / "kept"
    config = _auto_config(
        tmp_path, mode="gray", lineart_threshold=0.5, keep_images=keep_dir
    )

    _run_capture(config, monkeypatch, [_mixed_dark_faint_page()])

    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    assert kept.startswith(b"P5")
    raster = kept.split(b"\n", 3)[3]
    assert bytes([30]) in raster  # the dark print
    assert bytes([200]) in raster  # and the faint region, both intact


def test_sparse_full_window_page_survives_via_the_content_box_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # White backing, full window, one small printed block: the whole-frame
    # mean reads blank, so only the content-box measurement keeps the page.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    row_bytes = (frame_w + 7) // 8
    raster = bytearray(row_bytes * frame_h)
    for y in range(157, 177):  # a 60 x 5 mm block: 0.36% of the frame
        for x in range(118, 354):
            raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    frame = b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster)

    from scanmole.pnm import pnm_mean

    probe = tmp_path / "probe.pnm"
    probe.write_bytes(frame)
    whole = pnm_mean(probe)
    assert whole is not None and whole > 0.995  # blank by the frame mean

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files", _gray_window_scan(frame, dpi, window)
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    config = _auto_config(tmp_path, lineart_threshold=0.5, page_size="auto")
    stream = io.StringIO()

    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    page = next(e for e in events if e["event"] == "page")
    assert page["blank"] is False
    mean_value = page["mean"]
    assert isinstance(mean_value, float) and mean_value < 0.995  # the box mean


def test_faint_gray_content_without_a_content_box_is_not_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A light stamp in gray mode sits above the ink cutoff: no content box
    # exists, and the verdict must fall back to the whole-raster mean (the
    # box path would misread the page as an empty 1.0).
    dpi = 75
    scale = dpi / 25.4
    window = (210.0, 297.0)
    width, height = round(window[0] * scale), round(window[1] * scale)
    raster = bytearray([235]) * (width * height)
    for y in range(200, 500):
        for x in range(100, 500):
            raster[y * width + x] = 200  # faint, above the 0.5 ink cutoff
    frame = b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)

    from scanmole.pnm import pnm_content_stats

    probe = tmp_path / "probe.pnm"
    probe.write_bytes(frame)
    stats = pnm_content_stats(probe, min_ink_px=max(4, round(scale)))
    assert stats is not None and stats.bbox is None  # invisible to ink
    assert stats.mean == 1.0  # the box path would call it blank

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files", _gray_window_scan(frame, dpi, window)
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    config = _auto_config(
        tmp_path, mode="gray", lineart_threshold=0.5, page_size="auto"
    )
    stream = io.StringIO()

    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0

    page = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "page"
    )
    assert page["blank"] is False  # judged by the whole raster, kept


def test_fixed_page_size_bypasses_autocrop_and_content_sizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The escape hatch from automatic sizing: a fixed --page-size delivers
    # the frame exactly as scanned, backing borders and all.
    width, height = 120, 90
    rows = []
    for y in range(height):
        if 10 <= y <= 79:
            rows.append(bytes([110] * 30 + [250] * 60 + [110] * 30))
        else:
            rows.append(bytes([110] * width))
    frame = b"P5\n%d %d\n255\n" % (width, height) + b"".join(rows)

    keep_dir = tmp_path / "kept"
    config = _auto_config(
        tmp_path, mode="gray", lineart_threshold=0.5, keep_images=keep_dir
    )
    assert config.page_size == "a4"  # fixed, not auto

    _run_capture(config, monkeypatch, [frame])

    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    kept_w, kept_h = (int(v) for v in kept.split(b"\n")[1].split(b" "))
    assert (kept_w, kept_h) == (width, height)  # untouched by detection
