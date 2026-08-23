"""Helpers shared by the pipeline test modules."""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.events import EventWriter
from scanmole.pipeline import run_pipeline
from scanmole.scanner import (
    EffectiveSettings,
    ScanResult,
)

pytestmark = pytest.mark.integration

_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)


def _gray_page(path: Path) -> Path:
    # 40x40: large enough to stay above img2pdf's 3-point minimum at 300 dpi.
    path.write_bytes(b"P5\n40 40\n255\n" + bytes([120] * 1600))
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


def _gray_scan_pages(  # type: ignore[no-untyped-def]
    specs: list[bytes],
    faint_native: bool = False,
    settings: EffectiveSettings | None = None,
):
    negotiated = settings

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = negotiated or EffectiveSettings(
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
