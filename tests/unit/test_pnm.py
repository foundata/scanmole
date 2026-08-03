"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.pnm import pnm_mean


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
