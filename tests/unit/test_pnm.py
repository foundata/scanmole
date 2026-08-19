"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from scanmole.pnm import (
    adaptive_lineart_threshold,
    autocrop_image,
    autocrop_pnm,
    binarize_image,
    binarize_pnm,
    coherent_ink,
    crop_pnm,
    gray_histogram,
    image_mean,
    otsu_cut,
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


def test_failed_write_leaves_the_original_frame_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The frame may be the only copy of the paper: a full disk mid-write must
    # not truncate it. Writes go to a sibling temp file and replace atomically.
    original = b"P5\n8 1\n255\n" + bytes([0, 50, 100, 127, 128, 200, 255, 255])
    page = _write(tmp_path / "gray.pgm", original)
    real_write = Path.write_bytes

    def failing_write(self: Path, data: bytes) -> int:
        if self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    assert binarize_image(page, 0.5) is False  # best-effort wrapper reports it
    assert page.read_bytes() == original
    assert list(tmp_path.iterdir()) == [page]  # no staging leftovers


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


def test_autocrop_pnm_keeps_white_clipped_margins_and_near_edge_content(
    tmp_path: Path,
) -> None:
    # Some scanners white-clip a genuine lower paper margin to full
    # brightness, bit-identical to synthetic end-of-paper padding. No
    # image-only rule may strip it: here a black footer sits right above
    # the clipped margin and stripping "padding" would delete it.
    rows = []
    for y in range(100):
        if y in (58, 59):
            rows.append(bytes([0] * 100))  # the footer line
        elif y < 60:
            rows.append(bytes([230] * 100))
        else:
            rows.append(bytes([255] * 100))  # white-clipped margin
    original = b"P5\n100 100\n255\n" + b"".join(rows)
    page = _write(tmp_path / "clipped.pgm", original)

    assert autocrop_pnm(page, 4) is False  # no backing anywhere: keep whole
    assert page.read_bytes() == original


def test_autocrop_pnm_side_backing_never_resolves_a_white_bottom(
    tmp_path: Path,
) -> None:
    # Dark side backing resolves the width; the bottom rows are pure
    # uniform white (clipped margin or synthetic padding, unknowable).
    # Only the sides may be cropped, and trim applies only to the edges
    # the walk actually moved: the unresolved top and bottom edges keep
    # every row, proven by content sitting in the top two rows, which a
    # blanket trim would have deleted.
    paper = bytearray(bytes([80] * 8) + bytes([230] * 44) + bytes([80] * 8))
    edge_content = bytearray(paper)
    edge_content[12:20] = bytes(8)  # ink at the very top edge, row mean stays paper
    white = bytes([255] * 60)
    page = _write(
        tmp_path / "sides.pgm",
        b"P5\n60 60\n255\n" + bytes(edge_content) * 2 + bytes(paper) * 38 + white * 20,
    )

    assert autocrop_pnm(page, 2) is True

    data = page.read_bytes()
    assert data.startswith(b"P5\n40 60\n255\n")  # sides cropped and trimmed only
    raster = data.split(b"\n", 3)[3]
    assert raster[2:10] == bytes(8)  # the top-edge ink survived untrimmed


def _feeder_tail_frame(
    paper_rows: int = 100,
    left: int = 8,
    right: int = 51,
    height: int = 400,
    tail: int = 128,
) -> bytes:
    """A feeder frame: paper at the leading edge, dark sides, long tail."""
    row = bytearray([80] * 60)
    for column in range(left, right + 1):
        row[column] = 230
    tail_row = bytes([tail] * 60)
    return (
        b"P5\n60 %d\n255\n" % height
        + bytes(row) * paper_rows
        + tail_row * (height - paper_rows)
    )


def test_feeder_band_resolves_a_mid_gray_tail_dilution(tmp_path: Path) -> None:
    # The ADS-4550W simplex case: a huge window whose synthetic mid-gray
    # tail dominates every full-height column mean, so no column looks
    # like paper. The feeder-only leading-edge band re-derives the
    # columns from the paper region; the ordinary row walk then resolves
    # the tail normally.
    page = _write(tmp_path / "tail.pgm", _feeder_tail_frame())

    assert autocrop_pnm(page, 2, feeder_band_px=60) is True

    data = page.read_bytes()
    # Sides trimmed (moved), bottom resolved at the paper end and
    # trimmed (moved), top kept whole (unmoved).
    assert data.startswith(b"P5\n40 98\n255\n")


def test_mid_gray_tail_without_feeder_context_stays_unresolved(
    tmp_path: Path,
) -> None:
    # Without the explicit feeder context (flatbeds, unknown sources) the
    # fallback must not run: the frame stays whole exactly as before.
    original = _feeder_tail_frame()
    page = _write(tmp_path / "tail.pgm", original)

    assert autocrop_pnm(page, 2) is False
    assert page.read_bytes() == original


def test_feeder_band_handles_a_short_receipt(tmp_path: Path) -> None:
    # An ~80 mm receipt is shorter than the tail but longer than the
    # leading-edge band, so the band sees paper and the row walk stops
    # at the receipt's end.
    page = _write(
        tmp_path / "receipt.pgm",
        _feeder_tail_frame(paper_rows=95, left=15, right=46),
    )

    assert autocrop_pnm(page, 2, feeder_band_px=60) is True

    assert page.read_bytes().startswith(b"P5\n28 93\n255\n")


def test_feeder_band_fails_safely_on_an_all_dark_frame(tmp_path: Path) -> None:
    # Jammed feeder or full-bleed photo: the band finds no plausible
    # paper either, and the frame is kept whole.
    original = b"P5\n60 400\n255\n" + bytes([80] * 60) * 400
    page = _write(tmp_path / "dark.pgm", original)

    assert autocrop_pnm(page, 2, feeder_band_px=60) is False
    assert page.read_bytes() == original


def test_feeder_band_leaves_white_backing_frames_alone(tmp_path: Path) -> None:
    # White backing: the ordinary walk finds paper everywhere and exits
    # through the no-backing branch; the fallback never engages.
    original = b"P5\n60 400\n255\n" + bytes([250] * 60) * 400
    page = _write(tmp_path / "white.pgm", original)

    assert autocrop_pnm(page, 2, feeder_band_px=60) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_keeps_full_length_noisy_paper(tmp_path: Path) -> None:
    # No backing visible on any edge: nothing must be stripped.
    paper_row = bytes([250, 252] * 20)
    original = b"P5\n40 60\n255\n" + paper_row * 60
    page = _write(tmp_path / "full.pgm", original)

    assert autocrop_pnm(page, 0) is False
    assert page.read_bytes() == original


def test_trailing_raster_bytes_are_ignored_and_never_normalized(
    tmp_path: Path,
) -> None:
    # The fujitsu backend occasionally delivers one complete raster row
    # beyond the declared height. Every measurement must use exactly the
    # declared geometry, and a page that no processing step rewrites
    # must keep its bytes as delivered, trailing row included.
    body = bytes([0, 255] * 8)  # 4x4 checker-ish gray page
    exact = b"P5\n4 4\n255\n" + body
    extra_row = bytes([7, 7, 7, 7])
    padded = exact + extra_row
    clean = _write(tmp_path / "clean.pgm", exact)
    trailing = _write(tmp_path / "trailing.pgm", padded)

    assert pnm_mean(trailing) == pnm_mean(clean)  # the extra row never counts
    assert pnm_content_stats(trailing, min_ink_px=1) == pnm_content_stats(
        clean, min_ink_px=1
    )
    assert autocrop_pnm(trailing, 2) is False  # nothing to crop on this frame
    assert trailing.read_bytes() == padded  # untouched pages stay verbatim

    p4 = _write(tmp_path / "trailing.pbm", b"P4\n8 2\n" + bytes([0x00, 0xFF, 0xAA]))
    assert pnm_mean(p4) == pytest.approx(0.5)  # 8 white + 8 black, pad ignored


def test_rewrites_of_trailing_byte_pages_keep_declared_geometry(
    tmp_path: Path,
) -> None:
    # A page a processing step genuinely rewrites (binarization here) is
    # rebuilt from the declared geometry; the rewrite is caused by the
    # conversion, never by the harmless trailing bytes themselves.
    page = _write(
        tmp_path / "conv.pgm",
        b"P5\n8 1\n255\n" + bytes([0, 50, 100, 127, 128, 200, 255, 255]) + bytes(8),
    )

    assert binarize_pnm(page, 0.5) is True
    assert page.read_bytes() == b"P4\n8 1\n" + bytes([0b11110000])


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


def _dashed_lines(
    lines: list[int], *, x0: int = 100, x1: int = 820, tall: int = 24
) -> list[tuple[int, int, int, int]]:
    """Text-line stand-ins: rows of word-sized dashes with gaps between."""
    return [(x, y, x + 36, y + tall) for y in lines for x in range(x0, x1 - 36, 60)]


def test_coherent_ink_accepts_faint_text_lines() -> None:
    frame = _p4_frame(1000, 1400, _dashed_lines([300, 360, 420]))

    found = coherent_ink(frame, 300)

    assert found is not None
    x0, y0, x1, y1 = found.box
    assert x0 <= 100 and y0 <= 300 and x1 >= 800 and y1 >= 440
    assert found.mean < 1.0


def test_coherent_ink_accepts_a_page_number() -> None:
    digits = [(500, 1330, 512, 1360), (516, 1330, 528, 1360)]
    frame = _p4_frame(1000, 1400, digits)

    found = coherent_ink(frame, 300)

    assert found is not None
    x0, y0, x1, y1 = found.box
    assert x0 <= 500 and x1 >= 528 and y0 <= 1330 and y1 >= 1360
    assert found.mean < 0.9  # a compact region is mostly ink


def test_coherent_ink_rejects_a_uniform_blank() -> None:
    assert coherent_ink(_p4_frame(1000, 1400), 300) is None


def test_coherent_ink_rejects_scattered_noise() -> None:
    # Deterministic ~1% scatter: the per-tile density stays far below the
    # cut, exactly like binarized unimodal sensor noise.
    specks = [
        (x, y, x + 1, y + 1)
        for y in range(1400)
        for x in range(1000)
        if (x * 31 + y * 17) % 101 == 0
    ]
    assert coherent_ink(_p4_frame(1000, 1400, specks), 300) is None


def test_coherent_ink_rejects_thin_streaks() -> None:
    roller = _p4_frame(1000, 1400, [(300, 0, 302, 1400)])
    vertical_edge = _p4_frame(1000, 1400, [(0, 0, 2, 1400)])
    centered_bar = _p4_frame(1000, 1400, [(0, 700, 1000, 702)])
    straddling_bar = _p4_frame(1000, 1400, [(0, 707, 1000, 709)])

    assert coherent_ink(roller, 300) is None
    assert coherent_ink(vertical_edge, 300) is None
    assert coherent_ink(centered_bar, 300) is None
    assert coherent_ink(straddling_bar, 300) is None


def test_coherent_ink_rejects_the_pepper_page_otsu_accepts(tmp_path: Path) -> None:
    # The mandatory false-positive regression: paper at 235 with 1% of the
    # pixels at 170, randomly distributed. Otsu accepts the split (the
    # histogram is perfectly bimodal) and the projection bbox spans nearly
    # the whole frame, so only local coherence can tell it from text.
    width, height = 1000, 1400
    rng = random.Random(42)
    raster = bytearray([235]) * (width * height)
    for _ in range(width * height // 100):
        raster[rng.randrange(width * height)] = 170
    page = b"P5\n%d %d\n255\n" % (width, height) + bytes(raster)

    fraction = adaptive_lineart_threshold(page)
    assert fraction is not None and 0.6 < fraction < 0.75  # Otsu is fooled

    candidate = _write(tmp_path / "candidate.pgm", page)
    assert binarize_pnm(candidate, fraction) is True
    stats = pnm_content_stats(candidate, min_ink_px=4)
    assert stats is not None and stats.bbox is not None
    assert stats.bbox[2] - stats.bbox[0] > width * 0.9  # projections fooled too

    assert coherent_ink(candidate.read_bytes(), 300) is None  # coherence is not


def test_coherent_ink_ignores_non_p4_input() -> None:
    assert coherent_ink(b"P5\n4 4\n255\n" + bytes(16), 300) is None
    assert coherent_ink(b"not a pnm", 300) is None


def test_coherent_ink_mean_reflects_the_region_only() -> None:
    # A solid 200x120 block: the union box is tile-aligned around it, so
    # the mean must reflect mostly ink, not the surrounding white page.
    frame = _p4_frame(1000, 1400, [(200, 240, 400, 360)])

    found = coherent_ink(frame, 300)

    assert found is not None
    assert found.mean < 0.2


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


def _hist(spikes: dict[int, int]) -> list[int]:
    histogram = [0] * 256
    for value, count in spikes.items():
        histogram[value] = count
    return histogram


def test_otsu_cut_uses_the_t_plus_one_boundary() -> None:
    # Otsu assigns bin 100 to the dark class; the `value < cut` conversion
    # then needs cut 101 so that exactly the dark pixels turn black.
    assert otsu_cut(_hist({100: 300, 180: 700})) == 101


def test_otsu_rejects_uniform_and_narrow_histograms() -> None:
    uniform = [10] * 256
    narrow = _hist({200: 500, 205: 500})  # separation below the guard
    empty = [0] * 256
    single = _hist({240: 1000})

    assert otsu_cut(uniform) is None  # separability guard
    assert otsu_cut(narrow) is None
    assert otsu_cut(empty) is None
    assert otsu_cut(single) is None


def test_otsu_rejects_a_meaningless_class_weight() -> None:
    # 2 pixels of noise against 10000 of paper: not two populations.
    assert otsu_cut(_hist({30: 2, 240: 10000})) is None


def test_otsu_is_not_capped_at_seventy_percent() -> None:
    # Washed-out original: strokes at ~79% brightness. A 0.7 upper clamp
    # would push the cut below the strokes and lose them.
    cut = otsu_cut(_hist({201: 100, 245: 900}))

    assert cut == 202
    assert cut > round(0.7 * 255)


def test_adaptive_threshold_recovers_faint_next_to_dark_content() -> None:
    # Moderately dark content, faint strokes, bright paper: the fixed 0.5
    # cut (128) loses the faint strokes, the adaptive cut keeps both.
    page = b"P5\n100 100\n255\n" + bytes([110] * 1000 + [170] * 600 + [235] * 8400)

    fraction = adaptive_lineart_threshold(page)

    assert fraction is not None
    cut = round(fraction * 255)
    assert 170 < cut <= round(0.9 * 255)  # faint strokes fall on the ink side
    assert 0.5 * 255 < 170  # ...which the fixed cut would have lost


def test_adaptive_threshold_rejects_exploding_coverage() -> None:
    # Half the page darker than the split: a photo or backing, not text.
    page = b"P5\n100 100\n255\n" + bytes([100] * 6000 + [200] * 4000)

    assert adaptive_lineart_threshold(page) is None


def test_adaptive_threshold_rejects_large_gain_over_fixed() -> None:
    # Nothing below the fixed cut, but 30% of the page would become ink:
    # too big a jump to trust.
    page = b"P5\n100 100\n255\n" + bytes([140] * 3000 + [230] * 7000)

    assert adaptive_lineart_threshold(page) is None


def test_gray_histogram_channel_semantics() -> None:
    # P6 counts the green channel; 16-bit input counts the high bytes.
    color = b"P6\n2 1\n255\n" + bytes([255, 10, 255, 0, 250, 0])
    hist = gray_histogram(color)
    assert hist is not None
    assert hist[10] == 1 and hist[250] == 1 and sum(hist) == 2

    deep = b"P5\n2 1\n65535\n" + bytes([0x30, 0xFF, 0xE0, 0x01])
    hist = gray_histogram(deep)
    assert hist is not None
    assert hist[0x30] == 1 and hist[0xE0] == 1 and sum(hist) == 2


def test_gray_histogram_skips_p4_and_rejects_malformed() -> None:
    assert gray_histogram(b"P4\n8 1\n\x00") is None
    assert adaptive_lineart_threshold(b"P5\n4 2\n255\n" + bytes(3)) is None
    with pytest.raises(ValueError, match="truncated"):
        gray_histogram(b"P5\n4 2\n255\n" + bytes(3))
