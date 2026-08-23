"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest
from support.pipeline import (
    _NEEDS_IMG2PDF,
    _auto_config,
    _config,
    _gray_page,
    _gray_window_scan,
    _run_capture,
)

from scanmole.autocrop import autocrop_image
from scanmole.config import AutoSizePreference, ScanConfig
from scanmole.events import EventWriter
from scanmole.options import Capability, parse_capabilities
from scanmole.pipeline import run_pipeline
from scanmole.scanner import (
    EffectiveSettings,
    ScanResult,
    build_scan_command,
)
from scanmole.sheetflow import PageOrigin
from scanmole.sizing import PageContent, choose_crops

pytestmark = pytest.mark.integration


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
            source="ADF Duplex",
            mode="Lineart",
            resolution=dpi,
            window_mm=window,
            duplex=True,
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
            source="ADF Duplex",
            mode="Lineart",
            resolution=dpi,
            window_mm=window,
            duplex=True,
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
            source="ADF Duplex",
            mode="Lineart",
            resolution=dpi,
            window_mm=window,
            duplex=True,
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

    def recorder(
        page: Path, trim_px: int, feeder_band_px: int | None = None, *, dpi: int
    ) -> bool:
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
    # 8 px trim would have deleted it. The strip is short enough that its
    # column mean stays paper-bright, which is the localized content the
    # side walk promises to keep; the dense case it may drop is pinned by
    # test_dense_edge_adjacent_content_is_cropped_by_automatic_size.
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
            if 200 <= y < 400:  # short: the column mean stays paper-bright
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
    assert any(raster[row * row_bytes] != 0 for row in range(200, 400))


def _edge_strip_frame(
    dpi: int, window: tuple[float, float], backing: int, span: range
) -> tuple[int, bytes]:
    """A feeder frame with a 3 px dark strip 3 px inside the paper edge.

    ``span`` is the strip's vertical extent, which is what decides whether
    its column mean stays paper-bright or reads as backing.
    """
    scale = dpi / 25.4
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)
    paper_end = round(297 * scale)
    rows = []
    for y in range(frame_h):
        if y >= paper_end:
            rows.append(bytes([80] * frame_w))
            continue
        row = bytearray(
            [80] * backing + [230] * (frame_w - 2 * backing) + [80] * backing
        )
        if y in span:
            row[backing + 3 : backing + 6] = bytes(3)
        if 100 <= y < 1600:
            row[300:900] = bytes(600)  # dense block: keeps the page
        rows.append(bytes(row))
    return frame_w, b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)


def _run_with_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dpi: int,
    window: tuple[float, float],
    frame: bytes,
    page_size: str,
) -> tuple[int, bytes]:
    """Run the pipeline over one prepared frame and return the kept page."""

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
        page_size=page_size,
        source="adf",
        resolution=dpi,
        keep_images=keep_dir,
    )
    assert run_pipeline(config, EventWriter(enabled=False)) == 0
    kept = (keep_dir / "out" / "page_0001.pnm").read_bytes()
    _magic, dims, raster = kept.split(b"\n", 2)
    width, _height = map(int, dims.split())
    return width, raster


def test_dense_edge_adjacent_content_is_cropped_by_automatic_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The accepted limitation, stated as an expectation so it cannot drift
    # in either direction. The same strip as the trim test, but tall enough
    # that its column mean falls below the paper cutoff: with only 3 px of
    # bright margin ahead of it (far under the 2 mm minimum run) the side
    # walk cannot tell it from backing and crops it away. A fixed page size
    # is the remedy, pinned by the test below.
    dpi, window, backing = 150, (215.9, 355.6), 24
    frame_w, frame = _edge_strip_frame(dpi, window, backing, range(200, 800))

    width, raster = _run_with_frame(tmp_path, monkeypatch, dpi, window, frame, "auto")

    # 3 px of margin and the 3 px strip are gone beyond the ordinary trim.
    assert width == frame_w - 2 * backing - 4 - 6
    row_bytes = (width + 7) // 8
    assert all(raster[row * row_bytes] == 0 for row in range(200, 800))


def test_a_fixed_page_size_preserves_dense_edge_adjacent_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented escape hatch: a fixed page size never runs the
    # brightness walk, so the same frame keeps every column.
    dpi, window, backing = 150, (215.9, 355.6), 24
    frame_w, frame = _edge_strip_frame(dpi, window, backing, range(200, 800))

    width, raster = _run_with_frame(tmp_path, monkeypatch, dpi, window, frame, "a4")

    assert width == frame_w  # untouched: no crop at all
    row_bytes = (width + 7) // 8
    # Columns 24 to 31 land in byte 3: paper, then the 3 px strip, then paper.
    assert all(raster[row * row_bytes + 3] != 0 for row in range(200, 800))
    assert raster[100 * row_bytes + 3] == 0  # a row without the strip


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
            source="ADF Duplex",
            mode="Lineart",
            resolution=dpi,
            window_mm=window,
            duplex=True,
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


@_NEEDS_IMG2PDF
def test_the_resolution_applied_window_arms_content_sizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # End to end through the real negotiation: a backend that shrinks its
    # window once the dpi is applied (216x900 before, 200x300 at 100 dpi).
    # The frame comes back at the real window, so both axes read as
    # unresolved and the batch is content-sized. Carrying the stale
    # 216x900 instead would make the same frame look hardware-cropped and
    # silently skip sizing, which is the regression this pins.
    dpi = 100
    scale = dpi / 25.4
    frame_w, frame_h = round(200 * scale), round(300 * scale)
    row_bytes = (frame_w + 7) // 8
    raster = bytearray(row_bytes * frame_h)
    for y in range(round(20 * scale), round(200 * scale)):
        for x in range(round(20 * scale), round(150 * scale)):
            raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    frame = b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster)

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        applied = dict(settings)
        width, height = (
            (200.0, 300.0) if applied.get("--resolution") == "100" else (216.0, 900.0)
        )
        return {
            "resolution": Capability(kind="enum", choices=["100"]),
            "x": Capability(kind="range", minimum=0, maximum=width),
            "y": Capability(kind="range", minimum=0, maximum=height),
        }

    def fake_run_scanimage(command: list[str], on_page: object) -> tuple[int, str]:
        assert command[command.index("-y") + 1] == "300"
        batch = next(a for a in command if a.startswith("--batch=")).split("=", 1)[1]
        page = Path(batch % 1)
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return 7, ""

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run_scanimage)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        source="flatbed",
        resolution=dpi,
        page_size="auto",
    )

    with caplog.at_level("INFO", logger="scanmole.pipeline"):
        assert run_pipeline(config, EventWriter(enabled=False)) == 0

    messages = [record.getMessage() for record in caplog.records]
    assert any("sized" in text and "by content" in text for text in messages)


@_NEEDS_IMG2PDF
def test_unknown_source_evidence_keeps_unrelated_pages_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two consecutive simplex pages of different sizes on a device whose
    # source listing proves nothing. Pairing them would force one size on
    # both; each must keep its own.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)

    def white_frame(width_mm: float, height_mm: float, ink: float) -> bytes:
        width, height = round(window[0] * scale), round(window[1] * scale)
        row_bytes = (width + 7) // 8
        raster = bytearray(row_bytes * height)
        for y in range(round(5 * scale), round(height_mm * scale)):
            for x in range(round(5 * scale), round(width_mm * ink)):
                raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        return b"P4\n%d %d\n" % (width, height) + bytes(raster)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        # Derived from the real negotiation over a listing that proves
        # nothing about the source, so this exercises the actual verdict
        # rather than restating it.
        _command, negotiated = build_scan_command(
            config,
            device,
            {"resolution": Capability(kind="range", minimum=50, maximum=600)},
            str(work_dir / "page_%04d.pnm"),
        )
        assert negotiated.duplex is False
        settings = dataclasses.replace(
            negotiated, mode="Lineart", resolution=dpi, window_mm=window
        )
        assert callable(on_settings)
        on_settings(settings)
        pages = []
        for index, (width_mm, height_mm) in enumerate(
            ((200.0, 280.0), (140.0, 200.0)), 1
        ):
            page = work_dir / f"page_{index:04d}.pnm"
            page.write_bytes(white_frame(width_mm, height_mm, scale))
            pages.append(page)
            assert callable(on_page)
            on_page(page, PageOrigin(segment=1, frame=index))
        return ScanResult(pages=pages, settings=settings)

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
        source="adf-duplex",
        resolution=dpi,
        keep_images=keep_dir,
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    sizes = []
    for name in ("page_0001.pnm", "page_0002.pnm"):
        header = (keep_dir / "out" / name).read_bytes().split(b"\n", 2)
        sizes.append(tuple(map(int, header[1].split())))
    assert sizes[0] != sizes[1], "unrelated pages were fused into one sheet size"


def test_a_read_only_window_arms_content_sizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The device fixes its scan window and will not accept a value for it.
    # Automatic page size decides whether an axis is still at the window by
    # comparing against exactly that number, so losing it leaves every page
    # at full window width.
    caps = parse_capabilities(
        "    --resolution 300 [300] [read-only]\n"
        "    -x 0..50.8mm [50.8] [read-only]\n"
        "    -y 0..76.2mm [76.2] [read-only]\n"
    )
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source="adf",
        resolution=300,
        keep_images=tmp_path / "kept",
    )
    command, effective = build_scan_command(
        config, "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    assert "-x" not in command and "-y" not in command
    assert effective.window_mm == (50.8, 76.2)

    # A frame filling that window, paper-bright throughout, with one dense
    # block: both axes read as unresolved, so the page must be sized from
    # its content instead of kept at the window.
    frame_w, frame_h = 600, 900
    rows = []
    for y in range(frame_h):
        row = bytearray([235] * frame_w)
        if 100 <= y < 300:
            row[100:300] = bytes(200)
        rows.append(bytes(row))
    frame = b"P5\n%d %d\n255\n" % (frame_w, frame_h) + b"".join(rows)

    def fake_scan(
        scan_config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(effective)
        page = work_dir / "page_0001.pnm"
        page.write_bytes(frame)
        assert callable(on_page)
        on_page(page)
        return ScanResult(pages=[page], settings=effective)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    kept = (tmp_path / "kept" / "out" / "page_0001.pnm").read_bytes()
    width, height = (int(v) for v in kept.split(b"\n")[1].split(b" "))
    # Content-sized (444 x 477 px), not the 592 x 892 px the frame keeps
    # when the window is unknown and the sizing pass never arms.
    assert width < 500 and height < 600


def _read_only_source_settings(
    listing: str, tmp_path: Path, **overrides: object
) -> tuple[list[str], EffectiveSettings]:
    """Negotiate and build a command over a read-only listing."""
    caps = parse_capabilities(listing)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source="adf-duplex",
        mode="gray",
        resolution=300,
        **overrides,  # type: ignore[arg-type]
    )
    return build_scan_command(config, "test:0", caps, str(tmp_path / "page_%04d.pnm"))


def test_a_read_only_flatbed_places_pages_as_a_flatbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The request asked for a duplex feeder; the device is fixed to the
    # flatbed. Sizing must follow the device, and before this it fell back
    # to the request and placed pages as feeder frames.
    command, effective = _read_only_source_settings(
        "    --source Flatbed [Flatbed] [read-only]\n"
        "    --resolution 300 [300]\n"
        # A small declared window so the test frame fills it and both
        # axes read as unresolved, which is what feeds the size decision.
        "    -x 0..50.8mm [50.8] [read-only]\n"
        "    -y 0..76.2mm [76.2] [read-only]\n",
        tmp_path,
    )
    assert "--source" not in command
    assert "--batch-count=1" in command
    assert effective.source == "Flatbed"

    seen: list[bool] = []

    def record_placement(
        measured: list[PageContent],
        dpi: int,
        flatbed: bool,
        duplex: bool,
        preference: AutoSizePreference,
    ) -> object:
        seen.append(flatbed)
        return choose_crops(measured, dpi, flatbed, duplex, preference)

    monkeypatch.setattr("scanmole.pipeline.choose_crops", record_placement)
    _run_frame_with_settings(tmp_path, monkeypatch, effective, _window_frame(600, 900))

    assert seen == [True]


def test_a_read_only_feeder_enables_the_leading_band_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The band fallback is feeder-only and never inferred from pixels or
    # from the request. A conclusively read-only feeder is exactly the
    # positive evidence it asks for.
    _command, feeder = _read_only_source_settings(
        "    --source ADF Front [ADF Front] [read-only]\n"
        "    --resolution 300 [300]\n"
        # A small declared window so the test frame fills it and both
        # axes read as unresolved, which is what feeds the size decision.
        "    -x 0..50.8mm [50.8] [read-only]\n"
        "    -y 0..76.2mm [76.2] [read-only]\n",
        tmp_path,
    )
    assert feeder.source == "ADF Front"

    bands: list[int | None] = []

    def record_band(page: Path, trim_px: int, band_px: int | None, dpi: int) -> object:
        bands.append(band_px)
        return autocrop_image(page, trim_px, band_px, dpi=dpi)

    monkeypatch.setattr("scanmole.pipeline.autocrop_image", record_band)
    _run_frame_with_settings(tmp_path, monkeypatch, feeder, _window_frame(600, 900))

    assert bands == [max(1, round(50.0 * 300 / 25.4))]


def test_an_unknown_source_keeps_the_conservative_full_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same request, no source evidence at all: the fallback must stay off,
    # because a request is not the positive feeder evidence it needs.
    _command, unknown = _read_only_source_settings(
        "    --resolution 300 [300]\n"
        # A small declared window so the test frame fills it and both
        # axes read as unresolved, which is what feeds the size decision.
        "    -x 0..50.8mm [50.8] [read-only]\n"
        "    -y 0..76.2mm [76.2] [read-only]\n",
        tmp_path,
    )
    assert unknown.source is None

    bands: list[int | None] = []

    def record_band(page: Path, trim_px: int, band_px: int | None, dpi: int) -> object:
        bands.append(band_px)
        return autocrop_image(page, trim_px, band_px, dpi=dpi)

    monkeypatch.setattr("scanmole.pipeline.autocrop_image", record_band)
    _run_frame_with_settings(tmp_path, monkeypatch, unknown, _window_frame(600, 900))

    assert bands == [None]


def _window_frame(width: int, height: int) -> bytes:
    """A paper-bright frame filling the window, with one dense block."""
    rows = []
    for y in range(height):
        row = bytearray([235] * width)
        if 100 <= y < 300:
            row[100:300] = bytes(200)
        rows.append(bytes(row))
    return b"P5\n%d %d\n255\n" % (width, height) + b"".join(rows)


def _run_frame_with_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: EffectiveSettings,
    frame: bytes,
) -> None:
    """Run the pipeline over one frame with pre-negotiated settings."""

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
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        page_size="auto",
        source="adf-duplex",
        resolution=300,
    )
    assert run_pipeline(config, EventWriter(enabled=False)) == 0
