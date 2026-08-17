"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.pnm import (
    autocrop_image,
    autocrop_pnm,
    binarize_image,
    binarize_pnm,
    crop_pnm,
    image_mean,
    pnm_content_stats,
    pnm_mean,
)


def _bordered_page(paper: int = 250, backing: int = 110) -> bytes:
    """A 40x30 gray page: paper spans columns 5-34 and rows 3-24."""
    rows = []
    for y in range(30):
        if 3 <= y <= 24:
            rows.append(bytes([backing] * 5 + [paper] * 30 + [backing] * 5))
        else:
            rows.append(bytes([backing] * 40))
    return b"P5\n40 30\n255\n" + b"".join(rows)


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


def test_autocrop_pnm_crops_to_the_paper_box(tmp_path: Path) -> None:
    page = _write(tmp_path / "bordered.pgm", _bordered_page())

    assert autocrop_pnm(page, 0) is True

    data = page.read_bytes()
    assert data.startswith(b"P5\n30 22\n255\n")
    raster = data.split(b"\n", 3)[3]
    assert set(raster) == {250}  # only paper pixels remain


def test_autocrop_pnm_shaves_the_trim_inward(tmp_path: Path) -> None:
    page = _write(tmp_path / "bordered.pgm", _bordered_page())

    assert autocrop_pnm(page, 2) is True

    assert page.read_bytes().startswith(b"P5\n26 18\n255\n")


def test_autocrop_pnm_keeps_pages_without_a_border(tmp_path: Path) -> None:
    # White backing (or a borderless scan): every profile is paper-bright.
    original = b"P5\n40 30\n255\n" + bytes([250] * 1200)
    page = _write(tmp_path / "clean.pgm", original)

    assert autocrop_pnm(page, 4) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_keeps_a_page_with_no_detectable_paper(tmp_path: Path) -> None:
    # All-dark frame (jammed feeder, full-bleed photo): never crop to nothing.
    original = b"P5\n40 30\n255\n" + bytes([80] * 1200)
    page = _write(tmp_path / "dark.pgm", original)

    assert autocrop_pnm(page, 4) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_crops_color_pages(tmp_path: Path) -> None:
    # 40x30 RGB: same geometry as _bordered_page, encoded per channel.
    rows = []
    for y in range(30):
        if 3 <= y <= 24:
            row = [200, 110, 90] * 5 + [240, 250, 245] * 30 + [200, 110, 90] * 5
        else:
            row = [200, 110, 90] * 40
        rows.append(bytes(row))
    page = _write(tmp_path / "color.ppm", b"P6\n40 30\n255\n" + b"".join(rows))

    assert autocrop_pnm(page, 0) is True

    data = page.read_bytes()
    assert data.startswith(b"P6\n30 22\n255\n")
    raster = data.split(b"\n", 3)[3]
    assert len(raster) == 30 * 22 * 3
    assert raster[:3] == bytes([240, 250, 245])


def test_autocrop_pnm_strips_uniform_white_end_padding(tmp_path: Path) -> None:
    # Some devices pad past the paper end with pure white, indistinguishable
    # from paper by brightness. The padding is bit-perfectly uniform though;
    # real paper carries sensor noise (alternating 250/252 here).
    paper_row = bytes([250, 252] * 20)
    padding_row = bytes([255] * 40)
    page = _write(
        tmp_path / "padded.pgm",
        b"P5\n40 60\n255\n" + paper_row * 40 + padding_row * 20,
    )

    assert autocrop_pnm(page, 0) is True

    assert page.read_bytes() == b"P5\n40 40\n255\n" + paper_row * 40


def test_autocrop_pnm_keeps_full_length_noisy_paper(tmp_path: Path) -> None:
    # No uniform bottom row: nothing must be stripped from a full-length page.
    paper_row = bytes([250, 252] * 20)
    original = b"P5\n40 60\n255\n" + paper_row * 60
    page = _write(tmp_path / "full.pgm", original)

    assert autocrop_pnm(page, 0) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_skips_p4_and_non_pnm(tmp_path: Path) -> None:
    p4 = _write(tmp_path / "b.pbm", b"P4\n8 1\n\x00")
    png = _write(tmp_path / "n.png", b"\x89PNG\r\n\x1a\n" + bytes(16))

    assert autocrop_pnm(p4, 4) is False
    assert autocrop_pnm(png, 4) is False


def test_autocrop_image_keeps_a_malformed_page_with_a_warning(
    tmp_path: Path,
) -> None:
    original = b"P5\n40 30\n255\n" + bytes(10)  # truncated raster
    page = _write(tmp_path / "short.pgm", original)

    assert autocrop_image(page, 4) is False
    assert page.read_bytes() == original


def test_image_mean_skips_non_pnm_files(tmp_path: Path) -> None:
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    assert image_mean(png) is None


def _p4_frame(
    width: int, height: int, boxes: list[tuple[int, int, int, int]] | None = None
) -> bytes:
    """A white 1-bit frame with the given pixel boxes filled black."""
    row_bytes = (width + 7) // 8
    raster = bytearray(row_bytes * height)
    for x0, y0, x1, y1 in boxes or []:
        for y in range(y0, y1):
            for x in range(x0, x1):
                raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    return b"P4\n%d %d\n" % (width, height) + bytes(raster)


def test_content_stats_finds_the_content_box(tmp_path: Path) -> None:
    # 400x600 white frame, solid content block at (64, 100)-(320, 500).
    page = _write(tmp_path / "page.pbm", _p4_frame(400, 600, [(64, 100, 320, 500)]))

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.frame == (400, 600)
    assert stats.bbox == (64, 100, 320, 500)
    assert stats.mean < 0.1  # solid black content
    assert crop_pnm(page, stats.bbox) is True


def test_content_stats_empty_frame_has_no_box_and_reads_blank(
    tmp_path: Path,
) -> None:
    page = _write(tmp_path / "blank.pbm", _p4_frame(400, 600))

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox is None
    assert stats.mean == 1.0


def test_content_stats_ignores_hairline_streaks_and_specks(tmp_path: Path) -> None:
    # A 2-px roller streak over the full height and one speck row must not
    # widen the box beyond the real content block.
    page = _write(
        tmp_path / "streaky.pbm",
        _p4_frame(
            400,
            600,
            [
                (64, 100, 320, 500),  # real content
                (392, 0, 394, 600),  # right-edge streak, 2 px wide
                (30, 20, 42, 21),  # single speck row near the top
            ],
        ),
    )

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox == (64, 100, 320, 500)


def test_content_stats_reads_gray_frames(tmp_path: Path) -> None:
    # P5: dark block on white; ink is "darker than half brightness".
    rows = []
    for y in range(60):
        row = bytearray([255] * 80)
        if 20 <= y < 50:
            row[24:56] = bytes([30] * 32)
        rows.append(bytes(row))
    page = _write(tmp_path / "gray.pgm", b"P5\n80 60\n255\n" + b"".join(rows))

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox == (24, 20, 56, 50)
    assert stats.mean < 0.1


def test_content_stats_sparse_content_mean_stays_low(tmp_path: Path) -> None:
    # A small block on a huge white frame: the whole-frame mean would read
    # blank, the content-box mean must not.
    page = _write(
        tmp_path / "sparse.pbm", _p4_frame(2000, 3000, [(400, 400, 600, 480)])
    )

    stats = pnm_content_stats(page, min_ink_px=8)

    mean = pnm_mean(page)
    assert mean is not None and mean > 0.995  # would be dropped as blank
    assert stats is not None
    assert stats.bbox == (400, 400, 600, 480)
    assert stats.mean < 0.5


def test_content_stats_reach_covers_faint_content_below_the_box(
    tmp_path: Path,
) -> None:
    # A thin footer (a page number): too faint for the robust box, but the
    # permissive reach envelope must cover it so no crop can cut it off.
    page = _write(
        tmp_path / "footer.pbm",
        _p4_frame(
            400,
            900,
            [
                (64, 100, 320, 500),  # body
                (180, 800, 185, 806),  # faint footer: 5 ink per row
            ],
        ),
    )

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox == (64, 100, 320, 500)  # footer is no sizing evidence
    assert stats.reach is not None
    assert stats.reach[3] >= 806  # but it is inside the safety envelope


def test_content_stats_reach_excludes_hairline_streaks(tmp_path: Path) -> None:
    # A 2-px roller streak stays out of both envelopes: under 3 ink per row
    # and only one column bin wide.
    page = _write(
        tmp_path / "streak.pbm",
        _p4_frame(400, 600, [(64, 100, 320, 500), (392, 0, 394, 600)]),
    )

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox == (64, 100, 320, 500)
    assert stats.reach == (64, 100, 320, 500)


def test_content_stats_mean_ignores_ink_outside_the_box(tmp_path: Path) -> None:
    # The blank verdict must come from the box alone: heavy ink elsewhere in
    # the same rows (an eroded streak) may not darken a sparse page's mean.
    sparse = _write(
        tmp_path / "sparse.pbm", _p4_frame(2000, 3000, [(400, 400, 600, 480)])
    )
    streaky = _write(
        tmp_path / "streaky.pbm",
        _p4_frame(2000, 3000, [(400, 400, 600, 480), (1990, 0, 1992, 3000)]),
    )

    plain = pnm_content_stats(sparse, min_ink_px=8)
    with_streak = pnm_content_stats(streaky, min_ink_px=8)

    assert plain is not None and with_streak is not None
    assert plain.bbox == with_streak.bbox
    assert plain.mean == pytest.approx(with_streak.mean)


def test_crop_pnm_aligns_p4_origin_and_keeps_the_right_edge(tmp_path: Path) -> None:
    page = _write(tmp_path / "page.pbm", _p4_frame(400, 600, [(64, 100, 320, 500)]))

    # 70 aligns down to 64; the right edge stays exactly 330, so the width
    # grows only on the left (330 - 64 = 266). Rows are taken exactly.
    assert crop_pnm(page, (70, 100, 330, 500)) is True
    assert page.read_bytes().startswith(b"P4\n266 400\n")
    mean = pnm_mean(page)
    assert mean is not None and mean < 0.1  # the content block dominates


def test_crop_pnm_clears_padding_bits_when_ending_at_frame_edge(
    tmp_path: Path,
) -> None:
    # 12-px frame: cropping to the right edge leaves 4 padding bits, which
    # must come out white even though the source content is black there.
    page = _write(tmp_path / "edge.pbm", _p4_frame(12, 16, [(0, 0, 12, 16)]))

    assert crop_pnm(page, (8, 0, 12, 16)) is True
    assert page.read_bytes().startswith(b"P4\n4 16\n")
    assert pnm_mean(page) == pytest.approx(0.0)


def test_crop_pnm_rejects_full_frame_and_degenerate_boxes(tmp_path: Path) -> None:
    original = _p4_frame(64, 32, [(8, 8, 24, 24)])
    page = _write(tmp_path / "page.pbm", original)

    assert crop_pnm(page, (0, 0, 64, 32)) is False
    assert crop_pnm(page, (-10, -10, 200, 200)) is False  # clamps to full frame
    assert crop_pnm(page, (40, 10, 40, 20)) is False  # zero width
    assert page.read_bytes() == original


def test_crop_pnm_crops_gray_frames_pixel_exact(tmp_path: Path) -> None:
    rows = [bytes([y * 4 % 256] * 40) for y in range(30)]
    page = _write(tmp_path / "gray.pgm", b"P5\n40 30\n255\n" + b"".join(rows))

    assert crop_pnm(page, (5, 10, 25, 20)) is True

    data = page.read_bytes()
    assert data.startswith(b"P5\n20 10\n255\n")
    raster = data.split(b"\n", 3)[3]
    assert len(raster) == 20 * 10
    assert raster[0] == 40  # first kept row is source row 10 (10*4)
