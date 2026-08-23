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
import shutil
from pathlib import Path

import pytest
from support.pipeline import (
    _auto_config,
    _config,
    _gray_scan_pages,
    _gray_window_scan,
    _run_capture,
)

from scanmole.config import ScanConfig
from scanmole.errors import (
    DeviceError,
    ProcessingError,
)
from scanmole.events import EventWriter
from scanmole.negotiation import negotiate, resolve_faint_plan
from scanmole.options import Capability, parse_capabilities
from scanmole.pipeline import run_pipeline
from scanmole.scanner import (
    EffectiveSettings,
    ScanResult,
    build_scan_command,
)

pytestmark = pytest.mark.integration


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


def _faint_page() -> bytes:
    # Faint-only strokes at 170 on 235 paper: invisible to the fixed cut.
    return b"P5\n100 100\n255\n" + bytes([170] * 800 + [235] * 9200)


def _dark_page() -> bytes:
    return b"P5\n100 100\n255\n" + bytes([40] * 1500 + [235] * 8500)


def _true_blank() -> bytes:
    return b"P5\n100 100\n255\n" + bytes([240] * 10000)


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


def _read_only_lineart_caps(engaged: bool) -> dict[str, Capability]:
    """An Epson-shaped listing fixed in Lineart, TET on or parked."""
    return parse_capabilities(
        "    --source ADF|Flatbed [ADF]\n"
        "    --mode Lineart|Gray|Color [Lineart] [read-only]\n"
        "    --halftoning Text Enhanced Technology|Halftone A "
        f"[{'Text Enhanced Technology' if engaged else 'Halftone A'}] [read-only]\n"
        "    --resolution 300 [300]\n"
        "    -x 0..215.9mm [215.9]\n"
        "    -y 0..297.18mm [297.18]\n"
    )


def _negotiated_faint(caps: dict[str, Capability], tmp_path: Path) -> EffectiveSettings:
    """The settings a real faint negotiation plus command build produces."""
    plan = negotiate(
        caps,
        source="adf",
        mode="lineart",
        resolution=300,
        lineart_threshold="auto",
    )
    plan = resolve_faint_plan(plan, caps, lambda _settings: caps)
    command, effective = build_scan_command(
        dataclasses.replace(
            _config(images=None, output=tmp_path / "out.pdf"),
            source="adf",
            mode="lineart",
            lineart_threshold="auto",
            page_size="a4",
        ),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
        plan,
    )
    # The mode and the enhancement are both read-only here, so neither may
    # appear in argv however the negotiation classified them.
    assert "--mode" not in command
    assert "--halftoning" not in command
    assert not any(argument.startswith("--halftoning") for argument in command)
    return effective


def test_an_engaged_read_only_enhancement_carries_its_p4_frames_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end over the real negotiation: a device already sitting in
    # Lineart with TET engaged delivers the enhanced 1-bit result, and the
    # pipeline must accept it instead of refusing it as plain 1-bit.
    effective = _negotiated_faint(_read_only_lineart_caps(engaged=True), tmp_path)
    assert effective.faint_native is True
    # The mode was never emitted, but the device is in it and says so.
    assert effective.mode == "Lineart"

    native = b"P4\n16 16\n" + bytes([0xF0] * 2 * 16)
    keep_dir = tmp_path / "kept"
    config = _auto_config(tmp_path, keep_images=keep_dir)
    _run_capture(config, monkeypatch, [native], faint_native=effective.faint_native)

    assert (keep_dir / "out" / "page_0001.pnm").read_bytes() == native


def test_an_unengaged_read_only_enhancement_never_reaches_acquisition(
    tmp_path: Path,
) -> None:
    # The same read-only topology with the enhancement parked: the device
    # is conclusively fixed in plain 1-bit, so the request is refused
    # while building the command. The pipeline's own 1-bit backstop is for
    # capabilities that proved nothing, not for this.
    with pytest.raises(DeviceError, match="only plain 1-bit"):
        _negotiated_faint(_read_only_lineart_caps(engaged=False), tmp_path)


def test_a_read_only_gray_device_produces_adaptive_1_bit_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole path over the real negotiation: a device fixed in Gray
    # cannot be set to anything, delivers gray frames, and the guarded
    # adaptive threshold turns them into the 1-bit pages that were asked
    # for. Before this the request was UNKNOWN and best-effort.
    caps = parse_capabilities(
        "    --source ADF [ADF]\n"
        "    --mode Lineart|Gray|Color [Gray] [read-only]\n"
        "    --depth 8|16 [8]\n"
        "    --resolution 300 [300]\n"
    )
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"),
        source="adf",
        mode="lineart",
        lineart_threshold="auto",
        page_size="a4",
        keep_images=tmp_path / "kept",
    )
    plan = negotiate(
        caps,
        source=config.source,
        mode=config.mode,
        resolution=config.resolution,
        lineart_threshold=config.lineart_threshold,
    )
    plan = resolve_faint_plan(plan, caps, lambda _settings: caps)
    command, effective = build_scan_command(
        config, "test:0", caps, str(tmp_path / "page_%04d.pnm"), plan
    )

    assert "--mode" not in command
    assert command[command.index("--depth") + 1] == "8"
    assert effective.mode == "Gray"
    assert effective.faint_native is False

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _gray_scan_pages([_dark_page()], settings=effective),
    )
    monkeypatch.setattr(
        "scanmole.pipeline.build_pdf",
        lambda pages, output, dpi: output.write_bytes(b"%PDF-fake"),
    )

    assert run_pipeline(config, EventWriter(enabled=False)) == 0

    kept = (tmp_path / "kept" / "out" / "page_0001.pnm").read_bytes()
    assert kept.startswith(b"P4")  # gray in, adaptive 1-bit out
