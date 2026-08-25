"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import subprocess
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
    ProcessingError,
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


def _sparse_frame(
    width: int,
    height: int,
    *,
    words: bool = True,
    shadow_rows: int = 0,
    bar_px: int = 0,
    pepper_step: int = 0,
) -> bytes:
    """A white P5 frame with optional sparse content and scanner artifacts.

    ``words`` draws one short printed line (dash-shaped words with gaps).
    The artifacts mimic a full-width trailing-edge shadow band, a vertical
    scanner boundary bar and scattered sensor pepper, all calibrated dark
    enough for the ink mask yet subtle enough that the whole-page mean
    stays above the default blank threshold, exactly like the hardware
    artifacts on an otherwise blank page.
    """
    raster = bytearray(b"\xff" * (width * height))
    if words:
        # Stroke-like words, not solid bars: real print keeps most of its
        # line box white, and the rescue's solidity floor relies on that.
        for x in range(width // 6, width // 2, 120):
            for band in range(height // 3, height // 3 + 40, 8):
                for row in range(band, band + 2):
                    start = row * width + x
                    raster[start : start + 72] = b"\x28" * 72
    for row in range(height - shadow_rows, height):
        raster[row * width : (row + 1) * width] = b"\x64" * width
    if bar_px:
        for row in range(height):
            start = row * width
            raster[start : start + bar_px] = b"\x64" * bar_px
    if pepper_step:
        for index in range(0, width * height, pepper_step):
            raster[(index * 31) % (width * height)] = 100
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)


def _sparse_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> tuple[int, list[dict[str, object]], bytes | None]:
    """One fixed-size gray-mode run over ``frame``; returns exit, events, raster."""
    from support.pipeline import _gray_scan_pages

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _gray_scan_pages([frame]))
    rasters: list[bytes] = []

    def capture_pdf(pages: list[Path], output: Path, dpi: int | None) -> None:
        rasters.extend(page.read_bytes() for page in pages)
        output.write_bytes(b"%PDF-fake")

    monkeypatch.setattr("scanmole.pipeline.build_pdf", capture_pdf)
    stream = io.StringIO()
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), mode="gray"
    )
    try:
        code = run_pipeline(config, EventWriter(enabled=True, stream=stream))
    except NoPagesError:
        code = -1
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    return code, events, rasters[0] if rasters else None


def test_a_sparse_printed_line_survives_the_blank_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The P1 case: one genuine printed line diluted over a full page reads
    # above the default threshold and used to be dropped. The rescue must
    # keep it, report it nonblank with the evidence mean that explains
    # why, leave the raster byte-identical and change no event keys.
    frame = _sparse_frame(2480, 3508)

    code, events, raster = _sparse_run(tmp_path, monkeypatch, frame)

    assert code == 0  # the page survived; the run produced a PDF
    page_events = [event for event in events if event["event"] == "page"]
    assert len(page_events) == 1
    assert page_events[0]["blank"] is False
    mean = page_events[0]["mean"]
    assert isinstance(mean, float) and mean <= 0.995  # the evidence mean
    assert raster == frame  # the raster itself is untouched
    assert [event["event"] for event in events] == [
        "start",
        "page",
        "scan_done",
        "done",
    ]
    assert set(page_events[0]) == {"event", "n", "file", "blank", "mean"}


@pytest.mark.parametrize(
    "artifacts",
    [
        {"shadow_rows": 24},
        {"bar_px": 20},
        {"shadow_rows": 10, "bar_px": 8, "pepper_step": 1200},
        {"pepper_step": 600},
        {},
    ],
)
def test_scanner_artifacts_alone_never_rescue_a_blank_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifacts: dict[str, bool]
) -> None:
    # Trailing-edge shadows, boundary bars and binarized noise are not
    # content: a page carrying only those stays blank and dropped.
    frame = _sparse_frame(2480, 3508, words=False, **artifacts)
    from scanmole.pnm import pnm_mean  # the fixture must stay primary-blank

    probe = tmp_path / "probe.pgm"
    probe.write_bytes(frame)
    probe_mean = pnm_mean(probe)
    assert probe_mean is not None and probe_mean > 0.995

    code, events, _raster = _sparse_run(tmp_path, monkeypatch, frame)

    assert code == -1  # every page blank: the ordinary no-pages outcome
    page_events = [event for event in events if event["event"] == "page"]
    assert page_events and page_events[0]["blank"] is True


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


def test_keep_images_translates_a_blocked_destination(tmp_path: Path) -> None:
    # The archive root was replaced by a file: the OSError must come
    # back as the actionable processing failure, not a bare traceback.
    from scanmole.pipeline import copy_kept_images

    page = _gray_page(tmp_path / "a.pnm")
    blocked = tmp_path / "archive"
    blocked.write_text("a file where the folder should be")

    with pytest.raises(ProcessingError) as info:
        copy_kept_images([(1, page)], blocked, "scan")

    assert isinstance(info.value.__cause__, OSError)
    assert page.is_file()  # the original page is untouched


def test_keep_images_translates_an_uncreatable_destination(tmp_path: Path) -> None:
    # The destination vanished after validation and its place is now
    # blocked, so the parent chain cannot be created at all.
    from scanmole.pipeline import copy_kept_images

    page = _gray_page(tmp_path / "a.pnm")
    blocker = tmp_path / "gone"
    blocker.write_text("a file where the parent should be")

    with pytest.raises(ProcessingError) as info:
        copy_kept_images([(1, page)], blocker / "archive", "scan")

    assert isinstance(info.value.__cause__, OSError)


def test_keep_images_translates_a_readonly_destination(tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:  # pragma: no cover -- root ignores mode bits
        pytest.skip("permission failures cannot be provoked as root")
    from scanmole.pipeline import copy_kept_images

    page = _gray_page(tmp_path / "a.pnm")
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(ProcessingError) as info:
            copy_kept_images([(1, page)], parent / "archive", "scan")
    finally:
        parent.chmod(0o700)

    assert isinstance(info.value.__cause__, PermissionError)


@pytest.mark.parametrize("failing_call", [1, 2])
def test_keep_images_translates_a_failing_copy_and_removes_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    # A copy failing on the first or a later page (here: disk full)
    # translates with its cause, and the partial batch directory is
    # removed again: the authoritative originals survive in the
    # preserved work directory, so a half-copied archive would only
    # masquerade as a complete one.
    import shutil

    from scanmole.pipeline import copy_kept_images

    pages = [(1, _gray_page(tmp_path / "a.pnm")), (2, _gray_page(tmp_path / "b.pnm"))]
    archive = tmp_path / "archive"
    calls = {"count": 0}
    real_copy2 = shutil.copy2

    def failing_copy2(src: Path, dst: Path) -> object:
        calls["count"] += 1
        if calls["count"] == failing_call:
            raise OSError(28, "No space left on device", str(dst))
        return real_copy2(src, dst)

    monkeypatch.setattr("scanmole.pipeline.shutil.copy2", failing_copy2)

    with pytest.raises(ProcessingError, match="No space left") as info:
        copy_kept_images(pages, archive, "scan")

    assert isinstance(info.value.__cause__, OSError)
    assert not (archive / "scan").exists()  # no half-copied batch remains
    assert all(page.is_file() for _n, page in pages)  # originals untouched


def test_a_cleanup_failure_never_replaces_the_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Removing the partial archive is best-effort: when even that fails,
    # the caller still hears about the copy failure, which names what
    # actually went wrong.
    from scanmole.pipeline import copy_kept_images

    page = _gray_page(tmp_path / "a.pnm")
    archive = tmp_path / "archive"
    copy_error = OSError(28, "No space left on device")

    def failing_copy2(src: object, dst: object, **kwargs: object) -> object:
        raise copy_error

    def failing_rmtree(path: object, **kwargs: object) -> None:
        raise OSError(16, "Device or resource busy", str(path))

    monkeypatch.setattr("scanmole.pipeline.shutil.copy2", failing_copy2)
    monkeypatch.setattr("scanmole.pipeline.shutil.rmtree", failing_rmtree)

    with pytest.raises(ProcessingError, match="No space left") as info:
        copy_kept_images([(1, page)], archive, "scan")

    assert info.value.__cause__ is copy_error


def test_an_all_blank_run_reserves_no_archive_directory(tmp_path: Path) -> None:
    # Nothing was kept, so there is nothing to archive: neither the
    # destination nor an empty batch directory may appear before the
    # all-blank run fails.
    from scanmole.pipeline import copy_kept_images

    archive = tmp_path / "archive"
    copy_kept_images([], archive, "scan")
    assert not archive.exists()

    white = _white_page(tmp_path / "white.pgm")
    config = dataclasses.replace(
        _config((white,), tmp_path / "out.pdf"), keep_images=archive
    )
    with pytest.raises(NoPagesError):
        run_pipeline(config, EventWriter(enabled=False))
    assert not archive.exists()


@_NEEDS_IMG2PDF
def test_a_keep_images_failure_reaches_the_json_stream_as_code_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # What a frontend reads: the terminal error event classifies the
    # archive failure as a processing failure (exit 5), the events
    # before it are the ordinary ones, and the source images survive.
    from scanmole.cli import main

    page = _gray_page(tmp_path / "in.pnm")
    blocked = tmp_path / "archive"
    blocked.write_text("a file where the folder should be")

    code = main(
        [
            "--json",
            "--no-ocr",
            "--from-images",
            str(page),
            "--keep-images",
            str(blocked),
            "-o",
            str(tmp_path / "out.pdf"),
        ]
    )

    events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    kinds = [event["event"] for event in events]
    assert code == 5
    assert events[-1]["event"] == "error" and events[-1]["code"] == 5
    assert "page" in kinds and "scan_done" in kinds  # prior events unchanged
    assert "done" not in kinds
    assert page.is_file()  # the source image is preserved


def _no_skew(monkeypatch: pytest.MonkeyPatch, stderr: str = "") -> None:
    """Answer the host measurement without running Tesseract."""
    monkeypatch.setattr(
        "scanmole.deskew.run_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", stderr),
    )


def _deskew_scan(settings: EffectiveSettings, page: bytes | None = None):  # type: ignore[no-untyped-def]
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
        acquired = work_dir / "page_0001.pnm"
        if page is None:
            _gray_page(acquired)
        else:
            acquired.write_bytes(page)
        assert callable(on_page)
        on_page(acquired)
        return ScanResult(pages=[acquired], settings=settings)

    return fake_scan


def test_the_host_path_keeps_ocr_from_deskewing_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A backend without deskew hands the request to the host, and a page
    # the host owned is final: asking OCR to deskew it would resample it
    # a second time for nothing.
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
    _no_skew(monkeypatch)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), deskew=True, ocr=True
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    assert ocr_calls == [False]


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


def test_a_page_nothing_could_straighten_still_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 16-bit color is the one raster the host refuses, because rotating
    # it through Pillow would halve its depth. With OCR off nothing else
    # can straighten it, and the request must not be a silent no-op.
    settings = EffectiveSettings(
        source="ADF Duplex", mode="Color", resolution=300, deskew_applied=False
    )
    deep = b"P6\n40 40\n65535\n" + bytes(40 * 40 * 6)
    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _deskew_scan(settings, deep))
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    _no_skew(monkeypatch)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        deskew=True,
        ocr=False,
        mode="color",
        keep_blanks=True,
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


# Deskew ownership. Exactly one mechanism straightens a page: the backend
# where it took the request, the host on the raw frame otherwise, and
# ocrmypdf only for a batch the host owned no page of.


def _skewed_gray(width: int = 200, height: int = 260) -> bytes:
    """A gray page with enough ink for the measurement to be asked about."""
    rows = bytearray(b"\xff" * (width * height))
    for line in range(12):
        top = 30 + line * 18
        for y in range(top, top + 6):
            for x in range(24, 24 + 120 + (line % 3) * 20):
                rows[y * width + x] = 0
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(rows)


def _deskew_run(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deskew_applied: bool = False,
    stderr: str = "",
    page: bytes | None = None,
    **overrides: object,
):
    """One scanner run with the measurement answered from a recording."""
    calls: list[bool] = []
    required: list[list[str]] = []

    def fake_ocr(
        source: Path, output: Path, config: ScanConfig, deskew: bool = False
    ) -> None:
        calls.append(deskew)
        output.write_bytes(b"%PDF-fake")

    settings = EffectiveSettings(
        source="ADF Duplex",
        mode="Gray",
        resolution=300,
        deskew_applied=deskew_applied,
    )
    monkeypatch.setattr(
        "scanmole.pipeline.require_tools", lambda tools: required.append(list(tools))
    )
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _deskew_scan(settings, page if page is not None else _skewed_gray()),
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )
    monkeypatch.setattr("scanmole.pipeline.run_ocr", fake_ocr)
    _no_skew(monkeypatch, stderr)
    keep = tmp_path / "kept"
    settings_for_run: dict[str, object] = {
        "deskew": True,
        "ocr": True,
        "mode": "gray",
        "keep_images": keep,
        "keep_blanks": True,
    }
    settings_for_run.update(overrides)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        **settings_for_run,  # type: ignore[arg-type]
    )
    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    return calls, required, keep / "out" / "page_0001.pnm"


def test_the_backend_keeps_both_later_mechanisms_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing may straighten a page twice, so a backend that took the
    # request stops the host as well as ocrmypdf. It also means the run
    # never needs the measurement tool.
    calls, required, _page = _deskew_run(tmp_path, monkeypatch, deskew_applied=True)

    assert calls == [False]
    assert not any("tesseract" in tools for tools in required)


def _host_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every page the host deskew is actually handed."""
    seen: list[str] = []
    from scanmole.deskew import deskew_page as real

    def watched(page: Path) -> object:
        seen.append(page.name)
        return real(page)

    monkeypatch.setattr("scanmole.pipeline.deskew_page", watched)
    return seen


@pytest.mark.parametrize(
    ("backend_owns", "expected"),
    [
        pytest.param(True, 0, id="backend-owns"),
        pytest.param(False, 1, id="host-owns"),
    ],
)
def test_the_host_runs_exactly_where_the_backend_did_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_owns: bool,
    expected: int,
) -> None:
    # The whole point of settling ownership before acquisition: a page
    # the backend straightened must not be rotated again here, whether
    # the backend took the request because it was told to or because a
    # read-only option left no choice. Both arrive as the same boolean.
    seen = _host_calls(monkeypatch)

    _deskew_run(tmp_path, monkeypatch, deskew_applied=backend_owns)

    assert len(seen) == expected


def test_the_measurement_tool_is_required_before_any_paper_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host path needs Tesseract, and a run must find that out from
    # the negotiated settings rather than after a stack went through.
    _calls, required, _page = _deskew_run(tmp_path, monkeypatch)

    assert any("tesseract" in tools for tools in required)


@pytest.mark.parametrize("requested", [False, True])
def test_the_tool_is_only_required_where_the_host_would_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: bool
) -> None:
    # --no-deskew asks for nothing, so it must not turn a missing
    # Tesseract into a refused scan.
    _calls, required, _page = _deskew_run(tmp_path, monkeypatch, deskew=requested)

    assert any("tesseract" in tools for tools in required) is requested


def test_a_straightened_page_is_not_deskewed_by_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real angle: the host turns the page, so ocrmypdf must not.
    calls, _required, page = _deskew_run(
        tmp_path, monkeypatch, stderr="Deskew angle: 0.0350\n"
    )

    assert calls == [False]
    assert page.read_bytes().startswith(b"P5\n200 260\n")  # the canvas is kept


def test_a_page_the_host_cannot_own_falls_through_to_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 16-bit color is refused by the host, and no page in the batch was
    # straightened, so the fallback is still ocrmypdf's to take.
    deep = b"P6\n40 40\n65535\n" + bytes(40 * 40 * 6)
    calls, _required, _page = _deskew_run(
        tmp_path, monkeypatch, page=deep, mode="color"
    )

    assert calls == [True]


def test_a_fixed_page_size_keeps_its_canvas_while_still_straightening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixed size bypasses both automatic crops, so the configured
    # dimensions come through exactly; the page is still turned.
    _calls, _required, page = _deskew_run(
        tmp_path, monkeypatch, stderr="Deskew angle: 0.0350\n", page_size="a4"
    )

    assert page.read_bytes().startswith(b"P5\n200 260\n")


def test_the_second_crop_takes_the_wedges_the_rotation_straightens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A skewed sheet on dark backing: the first crop can only take the
    # bounding box, which still holds triangular backing in the corners.
    # Turning the page lines those wedges up along the edges, and the
    # second crop is what removes them. Without it they stay, so the
    # kept page's own outer lines are the test.
    from PIL import Image, ImageDraw

    width, height = 240, 300
    sheet = Image.new("L", (width, height), 0)  # backing
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([30, 30, width - 30, height - 30], fill=255)  # the paper
    for line in range(9):
        top = 60 + line * 22
        draw.rectangle([50, top, 50 + 120 + (line % 3) * 15, top + 7], fill=0)
    skewed = sheet.rotate(2.0, resample=Image.Resampling.BICUBIC, fillcolor=0)
    frame = tmp_path / "frame.pgm"
    skewed.save(frame)

    _calls, _required, page = _deskew_run(
        tmp_path,
        monkeypatch,
        stderr=f"Deskew angle: {-2.0 * 3.141592653589793 / 180:.6f}\n",
        page=frame.read_bytes(),
        page_size="auto",
    )

    data = page.read_bytes()
    tokens = data.split(b"\n", 3)
    kept_w, kept_h = (int(v) for v in tokens[1].split())
    raster = tokens[3]
    # Every outer line of what survives has to read as paper; a backing
    # wedge the second crop missed would darken one of them.
    edges = [
        sum(raster[y * kept_w] < 128 for y in range(kept_h)) / kept_h,
        sum(raster[y * kept_w + kept_w - 1] < 128 for y in range(kept_h)) / kept_h,
        sum(raster[x] < 128 for x in range(kept_w)) / kept_w,
        sum(raster[(kept_h - 1) * kept_w + x] < 128 for x in range(kept_w)) / kept_w,
    ]
    assert max(edges) < 0.30, edges
