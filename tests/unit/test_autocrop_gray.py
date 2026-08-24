"""Tests for gray and color paper-edge detection (policy, not PNM mechanics).

Parsing, padding, binarization and raw cropping stay with the raster
layer in ``test_pnm.py``; native 1-bit boundaries have their own rule and
their own module in ``test_autocrop_lineart.py``. What belongs here is
where a brightness profile puts the paper and what survives the crop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.autocrop import autocrop_image, autocrop_pnm
from scanmole.pnm import pnm_content_stats, pnm_mean

_PAPER, _BACKING, _INK, _CLIPPED = 230, 110, 20, 255


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _bordered_page(paper: int = 250, backing: int = 110) -> bytes:
    """A 40x30 gray page: paper spans columns 5-34 and rows 3-24."""
    rows = []
    for y in range(30):
        if 3 <= y <= 24:
            rows.append(bytes([backing] * 5 + [paper] * 30 + [backing] * 5))
        else:
            rows.append(bytes([backing] * 40))
    return b"P5\n40 30\n255\n" + b"".join(rows)


def test_autocrop_pnm_crops_to_the_paper_box(tmp_path: Path) -> None:
    page = _write(tmp_path / "bordered.pgm", _bordered_page())

    assert autocrop_pnm(page, 0, dpi=300) is True

    data = page.read_bytes()
    assert data.startswith(b"P5\n30 22\n255\n")
    raster = data.split(b"\n", 3)[3]
    assert set(raster) == {250}  # only paper pixels remain


def test_autocrop_pnm_shaves_the_trim_inward(tmp_path: Path) -> None:
    page = _write(tmp_path / "bordered.pgm", _bordered_page())

    assert autocrop_pnm(page, 2, dpi=300) is True

    assert page.read_bytes().startswith(b"P5\n26 18\n255\n")


def test_autocrop_pnm_keeps_pages_without_a_border(tmp_path: Path) -> None:
    # White backing (or a borderless scan): every profile is paper-bright.
    original = b"P5\n40 30\n255\n" + bytes([250] * 1200)
    page = _write(tmp_path / "clean.pgm", original)

    assert autocrop_pnm(page, 4, dpi=300) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_keeps_a_page_with_no_detectable_paper(tmp_path: Path) -> None:
    # All-dark frame (jammed feeder, full-bleed photo): never crop to nothing.
    original = b"P5\n40 30\n255\n" + bytes([80] * 1200)
    page = _write(tmp_path / "dark.pgm", original)

    assert autocrop_pnm(page, 4, dpi=300) is False
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

    assert autocrop_pnm(page, 0, dpi=300) is True

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

    assert autocrop_pnm(page, 4, dpi=300) is False  # no backing anywhere: keep whole
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

    assert autocrop_pnm(page, 2, dpi=300) is True

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

    assert autocrop_pnm(page, 2, feeder_band_px=60, dpi=300) is True

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

    assert autocrop_pnm(page, 2, dpi=300) is False
    assert page.read_bytes() == original


def test_feeder_band_handles_a_short_receipt(tmp_path: Path) -> None:
    # An ~80 mm receipt is shorter than the tail but longer than the
    # leading-edge band, so the band sees paper and the row walk stops
    # at the receipt's end.
    page = _write(
        tmp_path / "receipt.pgm",
        _feeder_tail_frame(paper_rows=95, left=15, right=46),
    )

    assert autocrop_pnm(page, 2, feeder_band_px=60, dpi=300) is True

    assert page.read_bytes().startswith(b"P5\n28 93\n255\n")


def test_feeder_band_fails_safely_on_an_all_dark_frame(tmp_path: Path) -> None:
    # Jammed feeder or full-bleed photo: the band finds no plausible
    # paper either, and the frame is kept whole.
    original = b"P5\n60 400\n255\n" + bytes([80] * 60) * 400
    page = _write(tmp_path / "dark.pgm", original)

    assert autocrop_pnm(page, 2, feeder_band_px=60, dpi=300) is False
    assert page.read_bytes() == original


def test_feeder_band_leaves_white_backing_frames_alone(tmp_path: Path) -> None:
    # White backing: the ordinary walk finds paper everywhere and exits
    # through the no-backing branch; the fallback never engages.
    original = b"P5\n60 400\n255\n" + bytes([250] * 60) * 400
    page = _write(tmp_path / "white.pgm", original)

    assert autocrop_pnm(page, 2, feeder_band_px=60, dpi=300) is False
    assert page.read_bytes() == original


def test_autocrop_pnm_keeps_full_length_noisy_paper(tmp_path: Path) -> None:
    # No backing visible on any edge: nothing must be stripped.
    paper_row = bytes([250, 252] * 20)
    original = b"P5\n40 60\n255\n" + paper_row * 60
    page = _write(tmp_path / "full.pgm", original)

    assert autocrop_pnm(page, 0, dpi=300) is False
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
    assert autocrop_pnm(trailing, 2, dpi=300) is False  # nothing to crop on this frame
    assert trailing.read_bytes() == padded  # untouched pages stay verbatim

    p4 = _write(tmp_path / "trailing.pbm", b"P4\n8 2\n" + bytes([0x00, 0xFF, 0xAA]))
    assert pnm_mean(p4) == pytest.approx(0.5)  # 8 white + 8 black, pad ignored


def test_autocrop_pnm_skips_non_pnm_files(tmp_path: Path) -> None:
    png = _write(tmp_path / "n.png", b"\x89PNG\r\n\x1a\n" + bytes(16))

    assert autocrop_pnm(png, 4, dpi=300) is False


def test_autocrop_image_keeps_a_malformed_page_with_a_warning(
    tmp_path: Path,
) -> None:
    original = b"P5\n40 30\n255\n" + bytes(10)  # truncated raster
    page = _write(tmp_path / "short.pgm", original)

    assert autocrop_image(page, 4, dpi=300) is False
    assert page.read_bytes() == original


def _column_frame(columns: list[int], height: int = 40) -> bytes:
    """A P5 frame whose every row repeats ``columns``."""
    return b"P5\n%d %d\n255\n" % (len(columns), height) + bytes(columns) * height


def _crop_width(tmp_path: Path, columns: list[int], *, dpi: int = 300) -> int:
    """Width left after an autocrop of a frame built from ``columns``."""
    page = tmp_path / "frame.pgm"
    page.write_bytes(_column_frame(columns))
    autocrop_pnm(page, 0, dpi=dpi)
    return int(page.read_bytes().split(b"\n", 2)[1].split()[0])


def test_side_walk_skips_a_saturated_strip_over_backing(tmp_path: Path) -> None:
    # The measured iX100 shape: a clipped sensor strip at the frame edge,
    # then backing, then paper. Without the sustained run the strip ends
    # the walk at once.
    columns = [_CLIPPED] * 19 + [_BACKING] * 60 + [_PAPER] * 400
    assert _crop_width(tmp_path, columns) == 400


def test_side_walk_skips_backing_that_touches_the_cutoff(tmp_path: Path) -> None:
    # The measured receipt shape: noisy backing whose mean reaches the
    # cutoff on isolated columns, which alone would end the walk at once.
    backing = [_BACKING] * 60
    for spike in (5, 23, 44):
        backing[spike] = 179  # a hair above 0.7 * 255
    assert _crop_width(tmp_path, backing + [_PAPER] * 400) == 400


def test_side_walk_keeps_an_alternating_pattern_at_the_paper_edge(
    tmp_path: Path,
) -> None:
    # A barcode reaching the paper edge: the gap never reads as backing,
    # so the outer paper-like edge is kept and nothing is discarded.
    bars: list[int] = []
    for _ in range(14):
        bars += [_INK] * 4 + [_PAPER] * 4
    columns = [_BACKING] * 40 + [_PAPER] * 8 + bars + [_PAPER] * 400
    assert _crop_width(tmp_path, columns) == 8 + len(bars) + 400


def test_side_walk_keeps_ordinary_text_near_the_paper_edge(tmp_path: Path) -> None:
    text: list[int] = []
    for _ in range(8):
        text += [_INK] * 3 + [_PAPER] * 12
    columns = [_BACKING] * 40 + [_PAPER] * 30 + text + [_PAPER] * 300
    assert _crop_width(tmp_path, columns) == 30 + len(text) + 300


def test_side_walk_drops_dense_content_behind_a_short_margin(tmp_path: Path) -> None:
    # The accepted limitation, pinned as an expectation: 6 px of bright
    # margin is far below the 2 mm run, so the dense block behind it fills
    # the gap exactly as backing would and goes with it.
    columns = [_BACKING] * 40 + [_PAPER] * 6 + [_INK] * 30 + [_PAPER] * 400
    assert _crop_width(tmp_path, columns) == 400


def test_side_walk_keeps_the_whole_edge_without_a_qualifying_run(
    tmp_path: Path,
) -> None:
    # Nothing stays paper-bright for the run: no crop rather than a guess.
    columns = [_BACKING] * 40 + [_PAPER] * 10 + [_BACKING] * 40 + [_PAPER] * 10
    page = tmp_path / "frame.pgm"
    original = _column_frame(columns)
    page.write_bytes(original)
    assert autocrop_pnm(page, 0, dpi=300) is False
    assert page.read_bytes() == original


def test_side_walk_treats_the_exact_cutoff_as_paper(tmp_path: Path) -> None:
    # The cutoff is 0.7 * 255 = 178.5, which no single sample can equal but
    # a column mean can. Alternating rows put the paper block exactly on it,
    # and the ">= cutoff" rule must accept it as paper.
    columns_low = [_BACKING] * 40 + [178] * 400
    columns_high = [_BACKING] * 40 + [179] * 400
    page = tmp_path / "cutoff.pgm"
    page.write_bytes(
        b"P5\n440 40\n255\n"
        + b"".join(bytes(columns_high if y % 2 else columns_low) for y in range(40))
    )
    autocrop_pnm(page, 0, dpi=300)
    assert int(page.read_bytes().split(b"\n", 2)[1].split()[0]) == 400

    # One step below the cutoff is backing, so no paper is found at all and
    # the frame is kept whole rather than cropped to a guess.
    below = tmp_path / "below.pgm"
    original = _column_frame([_BACKING] * 40 + [178] * 400)
    below.write_bytes(original)
    assert autocrop_pnm(below, 0, dpi=300) is False
    assert below.read_bytes() == original


def test_side_walk_run_is_physical_across_resolutions(tmp_path: Path) -> None:
    # The same physical layout at three resolutions must reach the same
    # millimetre verdict: a 1.6 mm clipped strip is always skipped.
    for dpi in (150, 300, 600):
        px = dpi / 25.4
        columns = (
            [_CLIPPED] * round(1.6 * px)
            + [_BACKING] * round(5.0 * px)
            + [_PAPER] * round(60.0 * px)
        )
        assert _crop_width(tmp_path, columns, dpi=dpi) == round(60.0 * px)


def test_side_walk_applies_to_both_directions(tmp_path: Path) -> None:
    columns = (
        [_CLIPPED] * 19
        + [_BACKING] * 40
        + [_PAPER] * 400
        + [_BACKING] * 40
        + [_CLIPPED] * 19
    )
    assert _crop_width(tmp_path, columns) == 400


def test_side_walk_handles_color_and_16_bit_frames(tmp_path: Path) -> None:
    columns = [_CLIPPED] * 19 + [_BACKING] * 60 + [_PAPER] * 400
    color = tmp_path / "color.ppm"
    color.write_bytes(
        b"P6\n%d 40\n255\n" % len(columns)
        + bytes(
            value for _ in range(40) for column in columns for value in (column,) * 3
        )
    )
    autocrop_pnm(color, 0, dpi=300)
    assert int(color.read_bytes().split(b"\n", 2)[1].split()[0]) == 400

    deep = tmp_path / "deep.pgm"
    deep.write_bytes(
        b"P5\n%d 40\n65535\n" % len(columns)
        + bytes(byte for _ in range(40) for column in columns for byte in (column, 0))
    )
    autocrop_pnm(deep, 0, dpi=300)
    assert int(deep.read_bytes().split(b"\n", 2)[1].split()[0]) == 400


def test_row_walk_is_unchanged_by_the_side_walk_evidence(tmp_path: Path) -> None:
    # The row profile gets the pathological shape the side walk now
    # rejects: a short bright run, a dark gap, then sustained paper. Rows
    # keep the original rule, so the first bright row still ends the walk
    # and those two rows survive.
    width = 400
    rows = (
        [_BACKING] * 4
        + [_PAPER] * 2
        + [_BACKING] * 30
        + [_PAPER] * 300
        + [_BACKING] * 4
    )
    page = tmp_path / "rows.pgm"
    page.write_bytes(
        b"P5\n%d %d\n255\n" % (width, len(rows))
        + b"".join(bytes([value] * width) for value in rows)
    )
    autocrop_pnm(page, 0, dpi=300)
    height = int(page.read_bytes().split(b"\n", 2)[1].split()[1])
    assert height == len(rows) - 8  # 4 backing rows off each end, nothing more


def test_gray_crop_stays_byte_identical(tmp_path: Path) -> None:
    # Pins the gray result against the 1-bit path landing beside it: the
    # dispatch reads the raster's own format, so a P5 frame must come out
    # of autocrop exactly as it always has, bytes included.
    page = _write(tmp_path / "bordered.pgm", _bordered_page())

    assert autocrop_pnm(page, 2, dpi=300) is True

    assert page.read_bytes() == b"P5\n26 18\n255\n" + bytes([250] * 26 * 18)
