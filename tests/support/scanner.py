"""Helpers shared by the scanner test modules."""

from __future__ import annotations

from pathlib import Path

from scanmole.config import ScanConfig


def _config(**overrides: object) -> ScanConfig:
    values: dict[str, object] = {
        "device": None,
        "source": "adf-duplex",
        "mode": "lineart",
        "resolution": 300,
        "page_size": "a4",
        "despeckle": 1,
        "deskew": False,
        "crop": False,
        "ocr": False,
        "lang": "deu",
        "rotate_pages": True,
        "optimize": 1,
        "pdfa": False,
        "blank_threshold": 0.995,
        "keep_blanks": False,
        "from_images": None,
        "keep_images": None,
        "output": Path("out.pdf"),
    }
    values.update(overrides)
    return ScanConfig(**values)  # type: ignore[arg-type]
