"""Tests for native 1-bit (``P4``) paper-edge detection.

The scanner thresholded these frames before ScanMole saw them, so the
evidence is ink density rather than brightness, and the rule that reads
it is not the brightness profile in ``test_autocrop_gray.py``. Parsing,
padding and raw bit cropping stay with the raster layer in
``test_pnm.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scanmole.autocrop import autocrop_image, autocrop_pnm
from scanmole.pnm import POPCOUNT


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# Every fixture is expressed in pixels at a stated dpi, because the rule's
# search distance, band height and paper run are all physical.

_LINEART_DPI = 150


def _lineart_frame(
    width: int,
    height: int,
    ink: Callable[[int, int], bool],
    *,
    dirty_padding: bool = False,
) -> bytes:
    """A ``P4`` frame whose black pixels are those ``ink(x, y)`` reports.

    ``dirty_padding`` fills the don't-care bits past the declared width,
    which the format permits and some producers really do.
    """
    row_bytes = (width + 7) // 8
    raster = bytearray(row_bytes * height)
    for y in range(height):
        for x in range(width):
            if ink(x, y):
                raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        if dirty_padding and width % 8:
            raster[y * row_bytes + row_bytes - 1] |= 0xFF >> (width % 8)
    return b"P4\n%d %d\n" % (width, height) + bytes(raster)


def _black(*regions: tuple[int, int, int, int]) -> Callable[[int, int], bool]:
    """An ``ink`` predicate for black rectangles, exclusive ends."""

    def ink(x: int, y: int) -> bool:
        return any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in regions)

    return ink


def _geometry(page: Path) -> tuple[int, int]:
    """Width and height from a PNM header."""
    tokens = page.read_bytes().split(b"\n", 2)[1].split()
    return int(tokens[0]), int(tokens[1])


def _lineart_page(tmp_path: Path, data: bytes) -> Path:
    return _write(tmp_path / "lineart.pbm", data)


@pytest.mark.parametrize(
    ("regions", "size"),
    [
        pytest.param(((10, 0, 16, 300),), (184, 300), id="left"),
        pytest.param(((184, 0, 190, 300),), (184, 300), id="right"),
        pytest.param(((0, 10, 200, 16),), (200, 284), id="top"),
        pytest.param(((0, 284, 200, 290),), (200, 284), id="bottom"),
        pytest.param(
            ((10, 0, 16, 300), (184, 0, 190, 300)), (168, 300), id="both-sides"
        ),
        pytest.param(
            ((0, 10, 200, 16), (0, 284, 200, 290)), (200, 268), id="both-ends"
        ),
    ],
)
def test_lineart_resolves_each_side_on_its_own_evidence(
    tmp_path: Path,
    regions: tuple[tuple[int, int, int, int], ...],
    size: tuple[int, int],
) -> None:
    # The measured native-lineart shape: an outer white sensor strip, the
    # dark boundary, then the page. Each side is judged alone, so a frame
    # with one border loses one border.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*regions)))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == size


@pytest.mark.parametrize("trim_px", [0, 4, 12])
def test_lineart_takes_no_transition_shave(tmp_path: Path, trim_px: int) -> None:
    # A thresholded edge has no half-gray transition pixels to lose, and
    # the walk already stops at the first position that reads as paper,
    # so the gray trim must not reach this path at any value.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black((10, 0, 16, 300))))

    assert autocrop_pnm(page, trim_px, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)


def test_lineart_removes_a_slanted_wedge_across_the_whole_axis(
    tmp_path: Path,
) -> None:
    # A boundary skewed by a fraction of a degree sits at a different
    # column in every band, so its whole-axis profile is far too flat to
    # find. The crop has to clear the innermost part of the wedge, not
    # the outermost, or half of it survives.
    wedge = [
        (10 + 5 * band, 50 * band, 16 + 5 * band, 50 * band + 50) for band in range(6)
    ]
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*wedge)))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (159, 300)  # exactly past the wedge at column 40
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert set(raster) == {0}  # the whole wedge is gone, not just its start


@pytest.mark.parametrize(
    ("missing", "cropped"),
    [
        pytest.param((2,), True, id="one-gap"),
        pytest.param((2, 4), False, id="two-gaps"),
    ],
)
def test_lineart_tolerates_gaps_up_to_the_span_threshold(
    tmp_path: Path, missing: tuple[int, ...], cropped: bool
) -> None:
    # A real boundary has gaps (punch holes, despeckle); an isolated mark
    # is nothing but gap. Five of six bands still resolve the side, four
    # of six no longer do.
    pieces = [
        (10, 50 * band, 16, 50 * band + 50) for band in range(6) if band not in missing
    ]
    original = _lineart_frame(200, 300, _black(*pieces))
    page = _lineart_page(tmp_path, original)

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is cropped

    assert _geometry(page) == ((184, 300) if cropped else (200, 300))
    if not cropped:
        assert page.read_bytes() == original


@pytest.mark.parametrize(
    ("inked_rows", "cropped"),
    [pytest.param(8, True, id="at-the-share"), pytest.param(7, False, id="below-it")],
)
def test_lineart_needs_the_measured_ink_share(
    tmp_path: Path, inked_rows: int, cropped: bool
) -> None:
    # Ink occupancy, not brightness: a boundary dithered to 80% of its
    # band is still a boundary, one at 70% is not evidence enough.
    original = _lineart_frame(
        200, 300, lambda x, y: 10 <= x < 16 and y % 10 < inked_rows
    )
    page = _lineart_page(tmp_path, original)

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is cropped

    if not cropped:
        assert page.read_bytes() == original


@pytest.mark.parametrize("dpi", [150, 300, 600])
def test_lineart_search_is_physical_across_resolutions(
    tmp_path: Path, dpi: int
) -> None:
    # Same 40 x 60 mm layout, three resolutions: 2 mm of white strip, a
    # 1 mm boundary, then paper. What comes off is the 3 mm boundary plus
    # at most the seven columns byte alignment costs.
    scale = dpi / 25.4
    width, height = round(40 * scale), round(60 * scale)
    page = _lineart_page(
        tmp_path,
        _lineart_frame(
            width, height, _black((round(2 * scale), 0, round(3 * scale), height))
        ),
    )

    assert autocrop_pnm(page, 4, dpi=dpi) is True

    kept_width, kept_height = _geometry(page)
    assert kept_height == height  # nothing spans the horizontal axis
    # Exactly the 3 mm, to within the pixel the layout rounds to; byte
    # alignment used to add up to seven columns on top.
    removed = (width - kept_width) / scale
    assert 3.0 - 1 / scale <= removed <= 3.0 + 1 / scale


def test_lineart_keeps_a_detected_left_edge_to_the_pixel(tmp_path: Path) -> None:
    # Rows are bit-packed, but the crop repacks them instead of slicing
    # from a byte, so a boundary ending at column 20 costs exactly the
    # columns through it. Rounding the edge to a byte either put the
    # boundary back into the page or gave away paper the detector had
    # just proved was paper.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black((14, 0, 21, 300))))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (179, 300)  # detected at 21 and kept there
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert set(raster) == {0}  # the whole boundary is gone, nothing else


def test_lineart_leaves_an_unresolved_left_edge_at_column_zero(
    tmp_path: Path,
) -> None:
    # Alignment applies to a detected edge only. A side the detector never
    # moved was never measured, may carry content in its outermost column,
    # and must not lose a pixel to byte alignment.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((0, 10, 200, 16), (0, 100, 2, 140))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (200, 284)
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert raster[(100 - 16) * 25] == 0xC0  # the mark in columns 0 and 1 survived


def test_lineart_rejects_a_crop_too_small_to_be_paper(tmp_path: Path) -> None:
    # Both sides resolve, on a frame whose remaining box is under the
    # minimum plausible paper. Nothing is cropped, and the exact bounds
    # are what the minimum is measured against.
    original = _lineart_frame(60, 100, _black((20, 0, 25, 100), (40, 0, 45, 100)))
    page = _lineart_page(tmp_path, original)

    assert autocrop_pnm(page, 4, dpi=60) is False
    assert page.read_bytes() == original


def test_lineart_clears_the_padding_bits_it_creates(tmp_path: Path) -> None:
    # The crop copies whole bytes, so the source's don't-care bits can end
    # up inside the new last byte column; they have to come out white.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(205, 300, _black((10, 0, 16, 300)), dirty_padding=True),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    width, _ = _geometry(page)
    assert width == 189
    row_bytes = (width + 7) // 8
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert not any(
        byte & (0xFF >> (width % 8)) for byte in raster[row_bytes - 1 :: row_bytes]
    )


def test_lineart_never_reads_padding_bits_as_ink(tmp_path: Path) -> None:
    # Three of four column bands carry the boundary, which is under the
    # span the rule wants. Counting the don't-care bits past column 156
    # would push the fourth band over and crop a page that has no top
    # border at all.
    original = _lineart_frame(157, 300, _black((0, 10, 149, 16)), dirty_padding=True)
    page = _lineart_page(tmp_path, original)

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is False
    assert page.read_bytes() == original

    spanning = _lineart_page(
        tmp_path, _lineart_frame(157, 300, _black((0, 10, 157, 16)))
    )
    assert autocrop_pnm(spanning, 4, dpi=_LINEART_DPI) is True
    assert _geometry(spanning) == (157, 284)  # the same frame with a full border


def test_lineart_uses_the_declared_geometry_and_keeps_trailing_bytes(
    tmp_path: Path,
) -> None:
    cropped = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((10, 0, 16, 300))) + b"\xff\xff\xff",
    )

    assert autocrop_pnm(cropped, 4, dpi=_LINEART_DPI) is True
    assert _geometry(cropped) == (184, 300)

    original = _lineart_frame(200, 300, _black()) + b"\xff\xff\xff"
    untouched = _write(tmp_path / "untouched.pbm", original)
    assert autocrop_pnm(untouched, 4, dpi=_LINEART_DPI) is False
    assert untouched.read_bytes() == original  # never normalized


@pytest.mark.parametrize(
    ("name", "ink"),
    [
        pytest.param("blank", _black(), id="all-white"),
        pytest.param("dark", lambda x, y: True, id="all-dark"),
        pytest.param("text", _black((2, 60, 28, 140)), id="edge-text"),
        pytest.param(
            "barcode",
            lambda x, y: 100 <= y < 180 and 4 <= x < 24 and x % 2 == 0,
            id="barcode-at-the-edge",
        ),
        pytest.param(
            "qr",
            lambda x, y: 100 <= y < 160 and 4 <= x < 64 and (x // 4 + y // 4) % 2 == 0,
            id="qr-at-the-edge",
        ),
        pytest.param("mark", _black((60, 0, 66, 50)), id="lone-deeper-mark"),
        pytest.param("vrule", _black((6, 120, 9, 190)), id="short-vertical-rule"),
        pytest.param("hrule", _black((40, 6, 110, 9)), id="short-horizontal-rule"),
        pytest.param("bar", _black((60, 0, 67, 300)), id="contradictory-evidence"),
    ],
)
def test_lineart_keeps_frames_without_qualifying_evidence(
    tmp_path: Path, name: str, ink: Callable[[int, int], bool]
) -> None:
    # An all-white or all-dark frame has no boundary to find. Print near
    # an edge is not one either as long as it leaves most of the
    # perpendicular axis alone, whatever its shape. The lone bar is read
    # as a boundary from both sides at once, and that contradiction keeps
    # the frame whole rather than cropping it to nothing.
    width = 100 if name == "bar" else 200
    original = _lineart_frame(width, 300, ink)
    page = _write(tmp_path / f"{name}.pbm", original)

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is False
    assert page.read_bytes() == original


def test_lineart_crops_dense_content_spanning_a_whole_edge(tmp_path: Path) -> None:
    # The accepted ambiguity, stated as a test: an intentional dense
    # border printed along the paper edge is exactly what scanner backing
    # looks like from the pixels, and automatic page size removes it.
    # A fixed page size is the escape hatch.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black((4, 0, 11, 300))))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (189, 300)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"P4\n200 300\n" + bytes(10), "truncated PNM raster", id="short"),
        pytest.param(b"P4\n8 x\n" + bytes(8), "bad PNM header", id="header"),
        pytest.param(b"P4\n0 5\n" + bytes(8), "bad PNM dimensions", id="dimensions"),
    ],
)
def test_lineart_follows_the_best_effort_policy_on_bad_input(
    tmp_path: Path, data: bytes, message: str
) -> None:
    page = _write(tmp_path / "bad.pbm", data)

    with pytest.raises(ValueError, match=message):
        autocrop_pnm(page, 4, dpi=_LINEART_DPI)

    assert autocrop_image(page, 4, dpi=_LINEART_DPI) is False
    assert page.read_bytes() == data


# Boundary coherence. Bands voting separately is not enough evidence: a
# single interior mark, deeper than the real boundary and unrelated to
# it, used to decide the crop and take most of the page with it. What
# follows pins that a crop only ever follows a boundary the bands agree
# on, and that disconnected content near an edge survives untouched.

_INK_PER_MARK = 6 * 50
"""Set pixels in the 6 x 50 px mark the fixtures below place off the edge."""


def _lineart_ink(page: Path) -> int:
    """Set pixels left in a rewritten ``P4`` file."""
    return sum(page.read_bytes().split(b"\n", 2)[2].translate(POPCOUNT))


@pytest.mark.parametrize(
    ("regions", "size", "ink"),
    [
        pytest.param(
            ((10, 0, 16, 250), (60, 0, 66, 50)), (184, 300), _INK_PER_MARK, id="left"
        ),
        pytest.param(
            ((184, 50, 190, 300), (134, 250, 140, 300)),
            (184, 300),
            _INK_PER_MARK,
            id="right",
        ),
        pytest.param(((0, 10, 160, 16), (0, 60, 40, 66)), (200, 284), 6 * 40, id="top"),
        pytest.param(
            ((0, 284, 160, 290), (160, 234, 200, 240)), (200, 284), 6 * 40, id="bottom"
        ),
    ],
)
def test_lineart_ignores_a_mark_deeper_than_the_boundary(
    tmp_path: Path,
    regions: tuple[tuple[int, int, int, int], ...],
    size: tuple[int, int],
    ink: int,
) -> None:
    # The measured over-crop: five bands hold the real boundary and one
    # of them also holds an isolated mark, 45 positions deeper. Taking
    # the deepest position any band found cut at the mark instead, threw
    # away about 9.5 mm of paper and deleted the mark with it.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*regions)))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == size
    assert _lineart_ink(page) == ink  # the mark itself survived the crop


@pytest.mark.parametrize("gap", [1, 2, 3, 4, 5, 6])
def test_lineart_ignores_a_mark_a_hair_off_the_boundary(
    tmp_path: Path, gap: int
) -> None:
    # One to six white columns between the boundary and the mark, so
    # their intervals sit within the skew allowance of each other and a
    # rule that groups darkness by proximity fuses them. Reaching the
    # mark still means stepping further than the allowance, so it lies
    # on no boundary path and cannot decide where the crop falls. The
    # four-column case is the measured one: it used to crop at column 32
    # and delete the mark with 2.7 mm of paper.
    mark = (16 + gap, 0, 22 + gap, 50)
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black((10, 0, 16, 300), mark))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == _INK_PER_MARK


@pytest.mark.parametrize(
    ("regions", "size", "ink"),
    [
        pytest.param(
            ((10, 0, 16, 300), (20, 0, 26, 50)), (184, 300), _INK_PER_MARK, id="left"
        ),
        pytest.param(
            ((184, 0, 190, 300), (174, 250, 180, 300)),
            (184, 300),
            _INK_PER_MARK,
            id="right",
        ),
        pytest.param(((0, 10, 160, 16), (0, 20, 40, 26)), (200, 284), 6 * 40, id="top"),
        pytest.param(
            ((0, 284, 160, 290), (160, 274, 200, 280)), (200, 284), 6 * 40, id="bottom"
        ),
    ],
)
def test_lineart_ignores_a_close_mark_on_every_side(
    tmp_path: Path,
    regions: tuple[tuple[int, int, int, int], ...],
    size: tuple[int, int],
    ink: int,
) -> None:
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*regions)))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == size
    assert _lineart_ink(page) == ink


@pytest.mark.parametrize("band", [0, 2, 5])
def test_lineart_ignores_a_close_mark_in_any_band(tmp_path: Path, band: int) -> None:
    top = band * 50
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((10, 0, 16, 300), (20, top, 26, top + 50))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == _INK_PER_MARK


def test_lineart_never_lets_a_mark_stand_in_for_a_missing_boundary(
    tmp_path: Path,
) -> None:
    # The boundary stops after five bands and a mark sits in the sixth,
    # near enough to look like its continuation. Counting it would buy
    # the sixth band's support at the price of cropping to the mark; the
    # path cannot reach it, so the five bands decide alone.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((10, 0, 16, 250), (20, 250, 26, 300))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == _INK_PER_MARK


def test_lineart_ignores_close_branches_below_boundary_support(
    tmp_path: Path,
) -> None:
    # Three branches in consecutive bands, each beside the boundary.
    # They reach each other, so they do form a path of their own; three
    # bands of six is still short of what a boundary needs.
    marks = [(20, band * 50, 26, band * 50 + 50) for band in (0, 1, 2)]
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black((10, 0, 16, 300), *marks))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == 3 * _INK_PER_MARK


def test_lineart_ignores_a_close_branch_on_a_slanted_boundary(
    tmp_path: Path,
) -> None:
    # The branch touches the wedge at the tolerance and would deepen the
    # cut by its own width if it counted. The wedge decides alone, so
    # the geometry is the wedge's own, branch or no branch.
    wedge = [
        (10 + 5 * band, 50 * band, 16 + 5 * band, 50 * band + 50) for band in range(6)
    ]
    plain = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*wedge)))
    assert autocrop_pnm(plain, 4, dpi=_LINEART_DPI) is True

    branched = _write(
        tmp_path / "branched.pbm",
        _lineart_frame(200, 300, _black(*wedge, (27, 100, 33, 150))),
    )
    assert autocrop_pnm(branched, 4, dpi=_LINEART_DPI) is True

    assert _geometry(branched) == _geometry(plain) == (159, 300)


@pytest.mark.parametrize("dpi", [150, 300, 600])
def test_lineart_close_mark_rejection_is_physical(tmp_path: Path, dpi: int) -> None:
    # The same 34 x 51 mm layout at three resolutions: a 1 mm boundary
    # 1.7 mm in and a 1 x 8 mm mark 0.7 mm behind it. The gap scales, so
    # the mark stays off the boundary path at every resolution.
    scale = dpi / 25.4
    width, height = round(34 * scale), round(51 * scale)
    mark = (round(3.4 * scale), 0, round(4.4 * scale), round(8 * scale))
    page = _lineart_page(
        tmp_path,
        _lineart_frame(
            width,
            height,
            _black((round(1.7 * scale), 0, round(2.7 * scale), height), mark),
        ),
    )

    assert autocrop_pnm(page, 4, dpi=dpi) is True

    kept_width, kept_height = _geometry(page)
    assert kept_height == height
    assert (width - kept_width) / scale == pytest.approx(2.7, abs=1 / scale)
    assert _lineart_ink(page) == (mark[2] - mark[0]) * (mark[3] - mark[1])


@pytest.mark.parametrize("band", [0, 2, 5])
def test_lineart_ignores_a_deeper_mark_in_any_band(tmp_path: Path, band: int) -> None:
    # First, middle and last band: no position in the band order is
    # special, because support comes from the neighbours either side.
    top = band * 50
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((10, 0, 16, 300), (60, top, 66, top + 50))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == _INK_PER_MARK


@pytest.mark.parametrize(
    "bands",
    [pytest.param((0, 2, 4), id="spread"), pytest.param((0, 1, 2), id="adjacent")],
)
def test_lineart_ignores_several_marks_below_boundary_support(
    tmp_path: Path, bands: tuple[int, ...]
) -> None:
    # Three marks, whether they sit apart or touch. Adjacent ones do form
    # one track, which is the point: it spans three bands of six and
    # still falls short of the boundary support the rule wants.
    marks = [(60, band * 50, 66, band * 50 + 50) for band in bands]
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black((10, 0, 16, 300), *marks))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (184, 300)
    assert _lineart_ink(page) == len(bands) * _INK_PER_MARK


def test_lineart_keeps_a_slanted_boundary_with_a_missing_band(
    tmp_path: Path,
) -> None:
    # The wedge again, with one band holding nothing at all. The track
    # bridges the hole and the whole wedge still comes off.
    wedge = [
        (10 + 5 * band, 50 * band, 16 + 5 * band, 50 * band + 50)
        for band in range(6)
        if band != 2
    ]
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black(*wedge)))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (159, 300)
    assert _lineart_ink(page) == 0  # the whole wedge is gone


def test_lineart_ignores_an_outlier_deeper_than_a_whole_wedge(
    tmp_path: Path,
) -> None:
    # The two rules have to hold together: the crop clears the innermost
    # point of the skewed boundary, and stops there, even though a
    # disconnected mark sits deeper than every point on it.
    wedge = [
        (10 + 5 * band, 50 * band, 16 + 5 * band, 50 * band + 50) for band in range(6)
    ]
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black(*wedge, (60, 100, 66, 150)))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (159, 300)
    assert _lineart_ink(page) == _INK_PER_MARK


@pytest.mark.parametrize("dpi", [150, 300, 600])
def test_lineart_outlier_rejection_is_physical(tmp_path: Path, dpi: int) -> None:
    # Same 34 x 51 mm layout at three resolutions: a 1 mm boundary 1.7 mm
    # in, and a 1 x 8 mm mark 10 mm in. The mark is disconnected at every
    # resolution because the tolerance scales with the band.
    scale = dpi / 25.4
    width, height = round(34 * scale), round(51 * scale)
    mark = (round(10 * scale), 0, round(11 * scale), round(8 * scale))
    page = _lineart_page(
        tmp_path,
        _lineart_frame(
            width,
            height,
            _black((round(1.7 * scale), 0, round(2.7 * scale), height), mark),
        ),
    )

    assert autocrop_pnm(page, 4, dpi=dpi) is True

    kept_width, kept_height = _geometry(page)
    assert kept_height == height
    assert (width - kept_width) / scale == pytest.approx(2.7, abs=1 / scale)
    assert _lineart_ink(page) == (mark[2] - mark[0]) * (mark[3] - mark[1])


def test_lineart_added_disconnected_content_never_crops_deeper(
    tmp_path: Path,
) -> None:
    # Metamorphic: the same frame, once plain and once with a localized
    # mark added off the paper edge. Adding content that cannot support a
    # boundary may not move the crop inward, and the content has to come
    # through the crop intact.
    boundary = _black((10, 0, 16, 300))
    plain = _lineart_page(tmp_path, _lineart_frame(200, 300, boundary))
    assert autocrop_pnm(plain, 4, dpi=_LINEART_DPI) is True

    for name, mark in (("far", (60, 100, 66, 150)), ("close", (20, 100, 26, 150))):
        added = _write(
            tmp_path / f"{name}.pbm",
            _lineart_frame(200, 300, _black((10, 0, 16, 300), mark)),
        )
        assert autocrop_pnm(added, 4, dpi=_LINEART_DPI) is True

        assert _geometry(added) == _geometry(plain)
        assert _lineart_ink(added) - _lineart_ink(plain) == _INK_PER_MARK


# Exact bounds. The detector finds a paper edge to the pixel, and the
# crop keeps it there: bit-packed rows are repacked rather than sliced
# from a byte, so neither side gives ground for the other.


@pytest.mark.parametrize("left", range(9, 17))
def test_lineart_keeps_every_left_edge_offset_exactly(
    tmp_path: Path, left: int
) -> None:
    # A boundary ending one column before each of the eight bit offsets.
    # Rounding the edge up to a byte used to cost the difference, up to
    # seven columns of paper, or 1.19 mm at this resolution.
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black((left - 6, 0, left, 300)))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (200 - left, 300)
    assert _lineart_ink(page) == 0  # the boundary is gone, no more, no less


@pytest.mark.parametrize("right", range(183, 191))
def test_lineart_keeps_every_right_edge_offset_exactly(
    tmp_path: Path, right: int
) -> None:
    # The mirror of the sweep above: the right edge was already exact and
    # has to stay that way.
    page = _lineart_page(
        tmp_path, _lineart_frame(200, 300, _black((right + 1, 0, right + 7, 300)))
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (right + 1, 300)
    assert _lineart_ink(page) == 0


def test_lineart_crops_both_sides_to_the_same_inset(tmp_path: Path) -> None:
    # The same distance off either side gives the same width, which is
    # what the byte-aligned left edge could not do.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((5, 0, 11, 300), (189, 0, 195, 300))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (189 - 11, 300)
    assert _lineart_ink(page) == 0


def test_lineart_keeps_content_on_an_unaligned_kept_column(tmp_path: Path) -> None:
    # A mark on the first and last column the crop keeps: an off-by-one
    # in the repacking shows up as a lost or shifted pixel, and the
    # geometry alone would not. It stays short enough for its column to
    # read as paper, or the cut would move past it.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(
            200,
            300,
            _black((7, 0, 13, 300), (13, 100, 14, 110), (199, 100, 200, 110)),
        ),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (187, 300)
    raster = page.read_bytes().split(b"\n", 2)[2]
    row_bytes = (187 + 7) // 8
    assert raster[100 * row_bytes] == 0x80  # the first kept column, in bit 7
    assert raster[100 * row_bytes + row_bytes - 1] == 0x20  # the last, 187 % 8 == 3


def test_lineart_exact_bounds_leave_unresolved_sides_at_the_frame(
    tmp_path: Path,
) -> None:
    # Only the top resolves; the sides were never measured and keep every
    # column, including the outermost one and whatever it carries.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(200, 300, _black((0, 10, 200, 16), (0, 100, 1, 140))),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    assert _geometry(page) == (200, 284)
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert raster[(100 - 16) * 25] == 0x80  # column 0 survived untouched


def test_lineart_never_repacks_the_source_padding(tmp_path: Path) -> None:
    # 157 px per row leaves five don't-care bits; a crop off a byte must
    # not shift them into the page.
    page = _lineart_page(
        tmp_path,
        _lineart_frame(157, 300, _black((3, 0, 9, 300)), dirty_padding=True),
    )

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    width, _height = _geometry(page)
    assert width == 148
    assert _lineart_ink(page) == 0  # neither the boundary nor the padding


def test_lineart_output_raster_matches_its_declared_geometry(tmp_path: Path) -> None:
    # The header and the bytes come from the same bounds; a crop that
    # narrowed one without the other would leave a short raster that
    # every later stage would misread.
    page = _lineart_page(tmp_path, _lineart_frame(200, 300, _black((3, 0, 10, 300))))

    assert autocrop_pnm(page, 4, dpi=_LINEART_DPI) is True

    width, height = _geometry(page)
    raster = page.read_bytes().split(b"\n", 2)[2]
    assert len(raster) == ((width + 7) // 8) * height
