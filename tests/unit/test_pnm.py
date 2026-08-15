"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.pnm import image_mean, pnm_mean


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


def test_image_mean_skips_non_pnm_files(tmp_path: Path) -> None:
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    assert image_mean(png) is None
