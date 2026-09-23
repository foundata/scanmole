"""Tests for the sparse-content blank rescue (no hardware, no tools).

The primary blank rule and the faint adaptive override are pinned in the
pipeline and faint suites; this module pins the second rescue: localized
coherent print can overturn a whole-page mean its own sparseness
diluted, and nothing else can.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.pipeline import _config

from scanmole.blankpage import sparse_rescue
from scanmole.config import ScanConfig


def _mm(dpi: int, millimetres: float) -> int:
    return max(1, round(millimetres * dpi / 25.4))


def _gray_frame(
    dpi: int,
    *,
    words_at: tuple[float, float] | None = (20.0, 60.0),
    shadow: bool = False,
    bar: bool = False,
    pepper: bool = False,
    texture: bool = False,
    holes: bool = False,
    broken_shadow: bool = False,
) -> bytes:
    """A 120 x 180 mm white frame with content or artifacts in gray.

    ``words_at`` places one printed line of five 6 x 3 mm words at the
    given position; the artifacts mirror a full-width trailing shadow, a
    full-height boundary bar, scattered pepper and light backing texture.
    """
    width, height = _mm(dpi, 120), _mm(dpi, 180)
    raster = bytearray(b"\xff" * (width * height))
    if texture:  # light backing texture: visible, but above the ink cut
        for index in range(0, width * height, 11):
            raster[index] = 200
    if words_at is not None:
        # Stroke-like words, not solid bars: real print keeps most of its
        # line box white, and the rescue's solidity floor relies on that.
        x0, y0 = _mm(dpi, words_at[0]), _mm(dpi, words_at[1])
        period = max(2, _mm(dpi, 0.7))
        stroke = max(1, period // 4)
        for word in range(5):
            left = x0 + word * _mm(dpi, 10)
            for band in range(5):
                top = y0 + band * period
                for row in range(top, top + stroke):
                    start = row * width + left
                    raster[start : start + _mm(dpi, 6)] = b"\x28" * _mm(dpi, 6)
    if shadow:
        for row in range(height - _mm(dpi, 2), height):
            raster[row * width : (row + 1) * width] = b"\x64" * width
    if bar:
        thick = _mm(dpi, 1.5)
        for row in range(height):
            start = row * width
            raster[start : start + thick] = b"\x64" * thick
    if pepper:
        for index in range(0, width * height, 331):
            raster[(index * 31) % (width * height)] = 100
    if holes:  # two solid punch holes near one edge, like a punched back
        for cy in (_mm(dpi, 60), _mm(dpi, 120)):
            cx = width - _mm(dpi, 10)
            radius = _mm(dpi, 3)
            for row in range(cy - radius, cy + radius):
                for col in range(cx - radius, cx + radius):
                    if (row - cy) ** 2 + (col - cx) ** 2 <= radius * radius:
                        raster[row * width + col] = 60
    if broken_shadow:  # a trailing shadow broken into short thin dashes
        row0 = height - _mm(dpi, 3)
        for left in range(0, width - _mm(dpi, 22), _mm(dpi, 28)):
            for row in range(row0, row0 + max(1, _mm(dpi, 0.6))):
                start = row * width + left
                raster[start : start + _mm(dpi, 20)] = b"\x64" * _mm(dpi, 20)
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)


def _as_p6(frame: bytes, ink: tuple[int, int, int] | None = None) -> bytes:
    """The gray frame as P6; ``ink`` colorizes the stroke samples."""
    header_end = frame.index(b"255\n") + 4
    header = frame[:header_end].replace(b"P5", b"P6", 1)
    out = bytearray(header)
    for sample in frame[header_end:]:
        if ink is not None and sample == 0x28:
            out += bytes(ink)
        else:
            out += bytes((sample, sample, sample))
    return bytes(out)


def _as_p4(frame: bytes) -> bytes:
    from scanmole.pnm import pnm_ink_mask

    mask = pnm_ink_mask(frame, 0.5)
    assert mask is not None
    return mask


def _dropped_config() -> ScanConfig:
    return _config(images=None, output=Path("out.pdf"))


def _write(tmp_path: Path, name: str, frame: bytes) -> Path:
    page = tmp_path / name
    page.write_bytes(frame)
    return page


_DROPPED = (False, True)
"""The primary verdict the rescue runs after: blank, not kept."""


@pytest.mark.parametrize("dpi", [150, 300, 600])
@pytest.mark.parametrize("kind", ["P4", "P5", "P6"])
def test_a_localized_printed_line_is_rescued(
    tmp_path: Path, dpi: int, kind: str
) -> None:
    frame = _gray_frame(dpi)
    converters: dict[str, Callable[[bytes], bytes]] = {
        "P4": _as_p4,
        "P5": lambda f: f,
        "P6": _as_p6,
    }
    frame = converters[kind](frame)
    page = _write(tmp_path, "page.pnm", frame)

    keep, blank, mean = sparse_rescue(page, _DROPPED, 0.9971, _dropped_config(), dpi)

    assert keep is True and blank is False
    assert mean is not None and mean <= 0.995  # the evidence explains itself
    assert page.read_bytes() == frame  # the raster is never touched


@pytest.mark.parametrize(
    "ink",
    [(0, 150, 0), (150, 0, 0), (0, 0, 150)],
)
def test_a_chromatic_printed_line_is_rescued(
    tmp_path: Path, ink: tuple[int, int, int]
) -> None:
    # The reproduction that green-channel evidence is blind to: a dark
    # chromatic line (dark green especially) leaves the green channel
    # bright, but its luminance is ink and the page is genuine content.
    page = _write(tmp_path, "page.ppm", _as_p6(_gray_frame(300), ink=ink))

    keep, blank, mean = sparse_rescue(page, _DROPPED, 0.9991, _dropped_config(), 300)

    assert keep is True and blank is False
    assert mean is not None and mean <= 0.995


def test_bright_chromatic_marks_stay_outside_the_guarantee(tmp_path: Path) -> None:
    # A luminous color (pure yellow) sits above the luminance cutoff:
    # the evidence mask cannot see it, and the page stays classified by
    # the mean alone, exactly as documented.
    page = _write(tmp_path, "page.ppm", _as_p6(_gray_frame(300), ink=(255, 255, 0)))

    keep, blank, _mean = sparse_rescue(page, _DROPPED, 0.999, _dropped_config(), 300)

    assert (keep, blank) == _DROPPED


@pytest.mark.parametrize(
    "position",
    [(80.0, 60.0), (20.0, 150.0), (70.0, 140.0)],
)
def test_translated_and_mirrored_content_is_still_found(
    tmp_path: Path, position: tuple[float, float]
) -> None:
    page = _write(tmp_path, "page.pnm", _gray_frame(300, words_at=position))

    keep, blank, _mean = sparse_rescue(page, _DROPPED, 0.9971, _dropped_config(), 300)

    assert keep is True and blank is False


@pytest.mark.parametrize(
    "artifacts",
    [
        {"shadow": True},
        {"bar": True},
        {"shadow": True, "bar": True},
        {"pepper": True},
        {"texture": True},
        {"shadow": True, "pepper": True, "texture": True},
        {"holes": True},
        {"broken_shadow": True},
        {},
    ],
)
def test_artifacts_and_noise_never_rescue(
    tmp_path: Path, artifacts: dict[str, bool]
) -> None:
    # Boundary bars and trailing shadows form dense coherent regions, but
    # they span an axis or stay hairlines; pepper never reaches tile
    # coherence and light backing texture never even enters the ink mask.
    frame = _gray_frame(300, words_at=None, **artifacts)
    page = _write(tmp_path, "page.pnm", frame)

    keep, blank, mean = sparse_rescue(page, _DROPPED, 0.9975, _dropped_config(), 300)

    assert (keep, blank) == _DROPPED
    assert mean == 0.9975  # the primary evidence stands untouched


def test_content_beside_artifacts_is_still_rescued(tmp_path: Path) -> None:
    # The localization filter discards the artifact regions and judges
    # what remains, so a genuine line does not need a clean page.
    frame = _gray_frame(300, shadow=True, bar=True)
    page = _write(tmp_path, "page.pnm", frame)

    keep, blank, mean = sparse_rescue(page, _DROPPED, 0.9971, _dropped_config(), 300)

    assert keep is True and blank is False
    assert mean is not None and mean <= 0.995


def test_an_unsupported_deep_color_page_keeps_its_verdict(tmp_path: Path) -> None:
    # A deep P6 outside the supported maxima yields no evidence at all:
    # the rescue must leave the primary verdict standing rather than
    # judge the page from a mask that guessed at lossy high bytes.
    width, height = 40, 30
    pixels = b"".join(
        (0).to_bytes(2, "big") * 3 if (x + y) % 7 == 0 else (280).to_bytes(2, "big") * 3
        for y in range(height)
        for x in range(width)
    )
    frame = b"P6\n%d %d\n300\n" % (width, height) + pixels
    page = _write(tmp_path, "deep.ppm", frame)

    keep, blank, mean = sparse_rescue(page, _DROPPED, 0.9971, _dropped_config(), 300)

    assert (keep, blank, mean) == (False, True, 0.9971)


def test_a_kept_page_and_disabled_detection_are_untouched(tmp_path: Path) -> None:
    page = _write(tmp_path, "page.pnm", _gray_frame(300))

    kept = sparse_rescue(page, (True, False), 0.99, _dropped_config(), 300)
    assert kept == (True, False, 0.99)

    kept_blank = sparse_rescue(page, (True, True), 0.9971, _dropped_config(), 300)
    assert kept_blank == (True, True, 0.9971)  # keep-blanks keeps its label

    disabled = dataclasses.replace(_dropped_config(), blank_threshold=0.0)
    unchanged = sparse_rescue(page, (False, True), 0.9971, disabled, 300)
    assert unchanged == (False, True, 0.9971)


def test_the_evidence_must_itself_pass_the_threshold(tmp_path: Path) -> None:
    # A strict threshold binds the rescue exactly like the primary rule:
    # evidence brighter than the configured cut cannot overturn it.
    page = _write(tmp_path, "page.pnm", _gray_frame(300))
    strict = dataclasses.replace(_dropped_config(), blank_threshold=0.05)

    keep, blank, _mean = sparse_rescue(page, _DROPPED, 0.9971, strict, 300)

    assert (keep, blank) == _DROPPED


def test_unreadable_pages_leave_the_verdict_standing(tmp_path: Path) -> None:
    truncated = _write(tmp_path, "page.pnm", b"P5\n100 100\n255\n\x00\x01")
    missing = tmp_path / "gone.pnm"
    foreign = _write(tmp_path, "page.png", b"\x89PNG\r\n\x1a\n not a pnm")

    for page in (truncated, missing, foreign):
        assert sparse_rescue(page, _DROPPED, 0.9971, _dropped_config(), 300) == (
            False,
            True,
            0.9971,
        )
