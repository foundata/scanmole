"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
import tempfile
from pathlib import Path

import pytest

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
        settings=EffectiveSettings(source=None, mode="Gray", resolution=None),
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
            settings=EffectiveSettings(source=None, mode="Gray", resolution=None),
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
    # sparse second page must survive blank detection via its content box
    # even though its whole-frame mean reads blank.
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

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    config = _config(images=None, output=tmp_path / "out.pdf")

    work_dirs_before = set(Path(tempfile.gettempdir()).glob("scanmole-*"))
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config, EventWriter(enabled=False))

    new_dirs = set(Path(tempfile.gettempdir()).glob("scanmole-*")) - work_dirs_before
    try:
        assert len(new_dirs) == 1
        assert (next(iter(new_dirs)) / "page_0001.pnm").is_file()
    finally:
        for directory in new_dirs:
            shutil.rmtree(directory, ignore_errors=True)


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


def _gray_scan_pages(specs: list[bytes]):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(source="ADF Duplex", mode="Gray", resolution=300)
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
    config: ScanConfig, monkeypatch: pytest.MonkeyPatch, specs: list[bytes]
) -> list[dict[str, object]]:
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _gray_scan_pages(specs))
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    stream = io.StringIO()
    assert run_pipeline(config, EventWriter(enabled=True, stream=stream)) == 0
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_auto_threshold_faint_page_still_drops_under_normal_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The blank verdict is the fixed-0.5 one: the faint-only backside stays
    # a blank and is dropped, exactly as with a numeric threshold.
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


def test_auto_threshold_leaves_native_p4_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = b"P4\n16 16\n" + bytes([0xF0] * 2 * 16)
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_images=keep_dir)

    _run_capture(config, monkeypatch, [native])

    assert (keep_dir / "out" / "page_0001.pnm").read_bytes() == native


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


def test_cli_accepts_auto_and_rejects_garbage(tmp_path: Path) -> None:
    from scanmole.cli import _build_config, build_parser

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
