"""Tests for stdlib PNM parsing and mean-brightness blank detection."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from scanmole.pnm import (
    adaptive_lineart_threshold,
    binarize_image,
    binarize_pnm,
    coherent_ink,
    crop_bit_rows,
    crop_pnm,
    gray_histogram,
    image_mean,
    otsu_cut,
    pnm_content_stats,
    pnm_mean,
)


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


def test_content_stats_thin_trailing_line_alone_is_no_sizing_evidence(
    tmp_path: Path,
) -> None:
    # A thin full-width line at the trailing frame edge is too flat to be a
    # plausible content block: alone it forms no robust box (it can never
    # pick a paper size), but it lands in the permissive reach, so no crop
    # may cut it. The accepted cost is an occasionally longer page.
    for rows in (2, 6):
        page = _write(
            tmp_path / f"trailing_{rows}.pbm",
            _p4_frame(800, 1000, [(0, 1000 - rows, 800, 1000)]),
        )

        stats = pnm_content_stats(page, min_ink_px=8)

        assert stats is not None
        assert stats.bbox is None  # never sizing evidence
        assert stats.reach is not None
        assert stats.reach[3] == 1000  # but never cut by a crop


def test_content_stats_mid_gray_trailing_shadow_is_invisible(
    tmp_path: Path,
) -> None:
    # The shadow band observed on real hardware sits around half brightness,
    # above the ink cutoff: it is invisible to both envelopes and cannot
    # even grow the crop.
    rows = []
    for y in range(600):
        row = bytearray([250] * 400)
        if 20 <= y < 50:
            row[24:56] = bytes([30] * 32)
        if y >= 596:
            row[:] = bytes([130] * 400)
        rows.append(bytes(row))
    page = _write(tmp_path / "shadow.pgm", b"P5\n400 600\n255\n" + b"".join(rows))

    stats = pnm_content_stats(page, min_ink_px=8)

    assert stats is not None
    assert stats.bbox == (24, 20, 56, 50)
    assert stats.reach == (24, 20, 56, 50)


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


def test_crop_pnm_honors_an_unaligned_p4_box_on_every_side(tmp_path: Path) -> None:
    page = _write(tmp_path / "page.pbm", _p4_frame(400, 600, [(64, 100, 320, 500)]))

    # 70 does not fall on a byte, and neither side gives ground for it:
    # the rows are repacked, so the width is exactly 330 - 70.
    assert crop_pnm(page, (70, 100, 330, 500)) is True
    assert page.read_bytes().startswith(b"P4\n260 400\n")
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


# ---- side-walk paper-run evidence (Candidate D) --------------------------


# Bit-exact P4 repacking. Rows are packed MSB first, so a crop that does
# not start on a byte has to move every pixel; what follows checks the
# shifting primitive against an obviously correct reference and pins the
# properties both callers rely on.


def _reference_crop(
    raster: bytes, *, row_bytes: int, box: tuple[int, int, int, int]
) -> bytes:
    """Unpack, slice, repack: slow, obvious, and the yardstick."""
    x0, y0, x1, y1 = box
    out_bytes = (x1 - x0 + 7) // 8
    out = bytearray()
    for y in range(y0, y1):
        row = bytearray(out_bytes)
        for index, x in enumerate(range(x0, x1)):
            byte = y * row_bytes + x // 8
            if x // 8 < row_bytes and byte < len(raster):
                if raster[byte] & (0x80 >> (x % 8)):
                    row[index // 8] |= 0x80 >> (index % 8)
        out += row
    return bytes(out)


def _pattern(width: int, height: int, seed: int) -> bytes:
    """A deterministic raster whose padding bits are deliberately dirty."""
    rng = random.Random(seed)
    row_bytes = (width + 7) // 8
    return bytes(rng.randrange(256) for _ in range(row_bytes * height))


def test_crop_bit_rows_matches_the_reference_exhaustively() -> None:
    # Every width through three bytes, every box inside it: this is where
    # the shift, the mask and the left-alignment have to agree with the
    # obvious implementation, bit for bit.
    checked = 0
    for width in range(1, 18):
        row_bytes = (width + 7) // 8
        for height in (1, 2):
            raster = _pattern(width, height, seed=width * 100 + height)
            for x0 in range(width):
                for x1 in range(x0 + 1, width + 1):
                    for y0 in range(height):
                        for y1 in range(y0 + 1, height + 1):
                            box = (x0, y0, x1, y1)
                            got = crop_bit_rows(raster, row_bytes=row_bytes, box=box)
                            want = _reference_crop(raster, row_bytes=row_bytes, box=box)
                            assert got == want, (width, height, box)
                            assert len(got) == (y1 - y0) * ((x1 - x0 + 7) // 8)
                            checked += 1
    assert checked > 2000  # the sweep really ran


def test_crop_bit_rows_matches_the_reference_on_larger_rasters() -> None:
    # Seeded, not random: the same cases every run, over rasters wide
    # enough that a box spans several source bytes.
    rng = random.Random(20260824)
    for _ in range(300):
        width = rng.randrange(9, 41)
        height = rng.randrange(1, 6)
        row_bytes = (width + 7) // 8
        raster = _pattern(width, height, seed=rng.randrange(1 << 30))
        x0 = rng.randrange(width)
        x1 = rng.randrange(x0 + 1, width + 1)
        y0 = rng.randrange(height)
        y1 = rng.randrange(y0 + 1, height + 1)
        box = (x0, y0, x1, y1)
        assert crop_bit_rows(raster, row_bytes=row_bytes, box=box) == _reference_crop(
            raster, row_bytes=row_bytes, box=box
        ), (width, height, box)


@pytest.mark.parametrize("offset", range(8))
def test_crop_bit_rows_moves_a_single_pixel_from_every_bit(offset: int) -> None:
    # One black pixel at every source bit in turn, cropped so it lands in
    # bit 7 of the output: the shift is what has to be exact.
    raster = bytes([0x80 >> offset, 0x00])
    got = crop_bit_rows(raster, row_bytes=2, box=(offset, 0, offset + 1, 1))
    assert got == bytes([0x80])


@pytest.mark.parametrize("width", range(1, 17))
def test_crop_bit_rows_clears_the_padding_it_creates(width: int) -> None:
    # Every output width, so every remainder modulo 8: the source is all
    # black, and the bits past the requested width must still be white.
    raster = bytes([0xFF] * 4)
    got = crop_bit_rows(raster, row_bytes=4, box=(3, 0, 3 + width, 1))
    out_bytes = (width + 7) // 8
    assert len(got) == out_bytes
    assert int.from_bytes(got, "big") == ((1 << width) - 1) << (out_bytes * 8 - width)


def test_crop_bit_rows_keeps_the_first_and_last_requested_columns() -> None:
    # Black exactly on both requested edges and white between them, so an
    # off-by-one in either direction shows up as a lost or gained pixel.
    raster = bytes([0b00100000, 0b00000100])
    got = crop_bit_rows(raster, row_bytes=2, box=(2, 0, 14, 1))
    assert got == bytes([0b10000000, 0b00010000])


def test_crop_bit_rows_never_reads_the_source_padding() -> None:
    # 12 px per row with every don't-care bit set. A crop to the declared
    # width must not turn those into image content.
    raster = bytes([0x00, 0x0F, 0x00, 0x0F])
    got = crop_bit_rows(raster, row_bytes=2, box=(1, 0, 12, 2))
    assert got == bytes([0x00, 0x00, 0x00, 0x00])


def test_crop_bit_rows_treats_missing_source_bits_as_white() -> None:
    # A box reaching past the row's bytes: the tail is white, never the
    # next row's pixels.
    raster = bytes([0xFF, 0xFF])
    got = crop_bit_rows(raster, row_bytes=1, box=(4, 0, 12, 1))
    assert got == bytes([0b11110000])


@pytest.mark.parametrize("x0", [0, 8, 16])
def test_crop_bit_rows_leaves_an_aligned_crop_byte_identical(x0: int) -> None:
    # The fast path has to be the same function as the slow one: an
    # aligned whole-byte box is a plain slice of the source.
    raster = _pattern(32, 3, seed=7)
    got = crop_bit_rows(raster, row_bytes=4, box=(x0, 0, x0 + 16, 3))
    want = b"".join(
        raster[row * 4 + x0 // 8 : row * 4 + x0 // 8 + 2] for row in range(3)
    )
    assert got == want


@pytest.mark.parametrize("x0", [1, 5])
def test_crop_pnm_mirrors_p4_geometry_left_and_right(tmp_path: Path, x0: int) -> None:
    # The same distance taken off either side gives the same width.
    # Rounding the left outward instead would break that symmetry.
    frame = _p4_frame(64, 8, [(20, 0, 44, 8)])
    left = _write(tmp_path / "left.pbm", frame)
    right = _write(tmp_path / "right.pbm", frame)

    assert crop_pnm(left, (x0, 0, 64, 8)) is True
    assert crop_pnm(right, (0, 0, 64 - x0, 8)) is True

    assert left.read_bytes().split(b"\n", 2)[1] == b"%d 8" % (64 - x0)
    assert right.read_bytes().split(b"\n", 2)[1] == b"%d 8" % (64 - x0)


def test_crop_pnm_keeps_an_odd_width_p4_exactly(tmp_path: Path) -> None:
    # 21 px per row (three bytes, three padding bits), cropped off a byte
    # on both sides.
    page = _write(tmp_path / "odd.pbm", _p4_frame(21, 4, [(3, 0, 18, 4)]))

    assert crop_pnm(page, (3, 1, 18, 3)) is True

    data = page.read_bytes()
    assert data.startswith(b"P4\n15 2\n")
    assert pnm_mean(page) == pytest.approx(0.0)  # every kept pixel is black
