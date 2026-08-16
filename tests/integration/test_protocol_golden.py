"""Golden test freezing the ``--json`` protocol (the CLI contract).

Runs the real CLI entry point on fixed inputs and compares the normalized
event stream against a committed transcript. A failing golden test is a
compatibility break for every frontend, not a test to update casually.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scanmole.cli import main

pytestmark = pytest.mark.integration

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden" / "from-images.jsonl"

_NEEDS_IMG2PDF = pytest.mark.skipif(
    shutil.which("img2pdf") is None, reason="img2pdf is not installed"
)


def _normalize(event: dict[str, object]) -> dict[str, object]:
    """Strip volatile fields (paths, size, duration) to stable placeholders."""
    normalized = dict(event)
    for key in ("file", "output"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = Path(value).name
    if "bytes" in normalized:
        assert isinstance(normalized["bytes"], int)
        assert normalized["bytes"] > 0
        normalized["bytes"] = "<SIZE>"
    if "seconds" in normalized:
        assert isinstance(normalized["seconds"], int | float)
        normalized["seconds"] = "<SECONDS>"
    if "version" in normalized:
        assert isinstance(normalized["version"], str)
        assert normalized["version"]
        normalized["version"] = "<VERSION>"
    return normalized


@_NEEDS_IMG2PDF
def test_json_stream_matches_the_golden_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gray = tmp_path / "page1.pgm"
    gray.write_bytes(b"P5\n4 4\n255\n" + bytes([120] * 16))
    white = tmp_path / "page2.pgm"
    white.write_bytes(b"P5\n4 4\n255\n" + bytes([255] * 16))

    exit_code = main(
        [
            "--json",
            "--from-images",
            str(gray),
            str(white),
            "--no-ocr",
            "-r",
            "300",
            "--page-size",
            "a4",
            "-l",
            "deu",
            "-o",
            str(tmp_path / "result.pdf"),
        ]
    )

    assert exit_code == 0
    got = [
        json.dumps(_normalize(json.loads(line)), sort_keys=True)
        for line in capsys.readouterr().out.splitlines()
    ]
    want = [
        json.dumps(json.loads(line), sort_keys=True)
        for line in GOLDEN.read_text().splitlines()
        if line.strip()
    ]
    assert got == want
