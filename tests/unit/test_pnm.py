"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.pnm import binarize_image, binarize_pnm, image_mean, pnm_mean


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_pnm_mean_all_white_p4_is_one(tmp_path: Path) -> None:
    # P4: one set bit is black; an all-zero raster is fully white.
    page = _write(tmp_path / "white.pbm", b"P4\n8 1\n" + bytes([0x00]))

    assert pnm_mean(page) == pytest.approx(1.0)


def test_pnm_mean_all_black_p4_is_zero(tmp_path: Path) -> None:
    page = _write(tmp_path / "black.pbm", b"P4\n8 1\n" + bytes([0xFF]))

    assert pnm_mean(page) == pytest.approx(0.0)


def test_pnm_mean_ignores_set_padding_bits_in_non_aligned_p4(tmp_path: Path) -> None:
    # 10 pixels per row need 2 bytes; the low 6 bits of the second byte are
    # padding. All pixels are white, every padding bit is set: still white.
    rows = bytes([0x00, 0x3F]) * 2
    page = _write(tmp_path / "padded.pbm", b"P4\n10 2\n" + rows)

    assert pnm_mean(page) == pytest.approx(1.0)


def test_pnm_mean_counts_black_pixels_regardless_of_padding(tmp_path: Path) -> None:
    # Same geometry, all 10 pixels black per row, padding bits set as well.
    rows = bytes([0xFF, 0xFF]) * 2
    page = _write(tmp_path / "black-padded.pbm", b"P4\n10 2\n" + rows)

    assert pnm_mean(page) == pytest.approx(0.0)


def test_pnm_mean_rejects_truncated_p4_raster(tmp_path: Path) -> None:
    # 10x2 needs 4 raster bytes; only 3 are present.
    page = _write(tmp_path / "short.pbm", b"P4\n10 2\n" + bytes(3))

    with pytest.raises(ValueError, match="truncated PNM raster"):
        pnm_mean(page)


def test_pnm_mean_rejects_truncated_p5_raster(tmp_path: Path) -> None:
    page = _write(tmp_path / "short.pgm", b"P5\n4 2\n255\n" + bytes(7))

    with pytest.raises(ValueError, match="truncated PNM raster"):
        pnm_mean(page)


def test_pnm_mean_rejects_non_numeric_dimensions(tmp_path: Path) -> None:
    page = _write(tmp_path / "dims.pbm", b"P4\nten 2\n" + bytes(4))

    with pytest.raises(ValueError, match="bad PNM dimensions"):
        pnm_mean(page)


def test_pnm_mean_rejects_zero_maxval(tmp_path: Path) -> None:
    page = _write(tmp_path / "maxval.pgm", b"P5\n2 1\n0\n" + bytes(2))

    with pytest.raises(ValueError, match="bad PNM maxval"):
        pnm_mean(page)


def test_pnm_mean_gray_p5_is_half(tmp_path: Path) -> None:
    page = _write(
        tmp_path / "gray.pgm", b"P5\n4 1\n255\n" + bytes([128, 128, 128, 128])
    )

    assert pnm_mean(page) == pytest.approx(128 / 255, abs=1e-6)


def test_pnm_mean_color_p6_averages_all_channels(tmp_path: Path) -> None:
    page = _write(tmp_path / "c.ppm", b"P6\n1 1\n255\n" + bytes([0, 128, 255]))

    assert pnm_mean(page) == pytest.approx((0 + 128 + 255) / (3 * 255), abs=1e-6)


def test_pnm_mean_handles_header_comment(tmp_path: Path) -> None:
    page = _write(
        tmp_path / "commented.pgm", b"P5\n# a comment\n2 1\n255\n" + bytes([255, 255])
    )

    assert pnm_mean(page) == pytest.approx(1.0)


def test_pnm_mean_returns_none_for_non_pnm(tmp_path: Path) -> None:
    page = _write(tmp_path / "not.png", b"\x89PNG\r\n\x1a\n" + bytes(16))

    assert pnm_mean(page) is None


def test_pnm_mean_rejects_truncated_header(tmp_path: Path) -> None:
    # Long enough to pass the minimum-size guard, but the maxval token is missing.
    page = _write(tmp_path / "bad.pgm", b"P5\n12 34  ")

    with pytest.raises(ValueError, match="truncated PNM header"):
        pnm_mean(page)


def test_binarize_pnm_thresholds_gray_to_p4(tmp_path: Path) -> None:
    # 8x1: four dark pixels (below 50% of 255 = cut 128), four bright ones.
    page = _write(
        tmp_path / "gray.pgm",
        b"P5\n8 1\n255\n" + bytes([0, 50, 100, 127, 128, 200, 255, 255]),
    )

    assert binarize_pnm(page, 0.5) is True
    assert page.read_bytes() == b"P4\n8 1\n" + bytes([0b11110000])


def test_binarize_pnm_pads_non_aligned_rows_with_white(tmp_path: Path) -> None:
    # 10x2 all black: padding bits must stay zero so pnm_mean sees pure black.
    page = _write(tmp_path / "wide.pgm", b"P5\n10 2\n255\n" + bytes(20))

    assert binarize_pnm(page, 0.5) is True
    assert page.read_bytes() == b"P4\n10 2\n" + bytes([0xFF, 0xC0, 0xFF, 0xC0])
    assert pnm_mean(page) == pytest.approx(0.0)


def test_binarize_pnm_uses_the_green_channel_for_color(tmp_path: Path) -> None:
    # Pixel 1: dark green -> black; pixel 2: bright green -> white. The other
    # channels are set to mislead a naive average.
    page = _write(
        tmp_path / "c.ppm", b"P6\n2 1\n255\n" + bytes([255, 10, 255, 0, 250, 0])
    )

    assert binarize_pnm(page, 0.5) is True
    assert page.read_bytes() == b"P4\n2 1\n" + bytes([0b10000000])


def test_binarize_pnm_leaves_p4_and_non_pnm_alone(tmp_path: Path) -> None:
    p4 = _write(tmp_path / "already.pbm", b"P4\n8 1\n\x00")
    png = _write(tmp_path / "not.png", b"\x89PNG\r\n\x1a\n" + bytes(16))

    assert binarize_pnm(p4, 0.5) is False
    assert binarize_pnm(png, 0.5) is False
    assert p4.read_bytes() == b"P4\n8 1\n\x00"


def test_binarize_pnm_rejects_truncated_raster(tmp_path: Path) -> None:
    page = _write(tmp_path / "short.pgm", b"P5\n4 2\n255\n" + bytes(7))

    with pytest.raises(ValueError, match="truncated PNM raster"):
        binarize_pnm(page, 0.5)


def test_binarize_image_keeps_a_malformed_page_with_a_warning(
    tmp_path: Path,
) -> None:
    original = b"P5\n4 2\n255\n" + bytes(7)
    page = _write(tmp_path / "short.pgm", original)

    assert binarize_image(page, 0.5) is False
    assert page.read_bytes() == original


def test_image_mean_skips_non_pnm_files(tmp_path: Path) -> None:
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    assert image_mean(png) is None
