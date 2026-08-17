"""Tests for the content-based page size decisions (pure logic, no files)."""

from __future__ import annotations

from pathlib import Path

from scanmole.sizing import PageContent, choose_crops

_DPI = 300
_SCALE = _DPI / 25.4

# A full DS-730N-style scan window: 215.9 x 393.7 mm at 300 dpi.
_FRAME = (2550, 4650)


def _mm(value: float) -> int:
    return round(value * _SCALE)


def _page(
    number: int, bbox_mm: tuple[float, float, float, float] | None
) -> PageContent:
    bbox_px = tuple(_mm(value) for value in bbox_mm) if bbox_mm is not None else None
    return PageContent(
        number=number,
        path=Path(f"page_{number:04d}.pnm"),
        frame_px=_FRAME,
        bbox_px=bbox_px,  # type: ignore[arg-type]
    )


def test_a4_content_snaps_to_a4_not_letter() -> None:
    # US letter has a smaller area than A4 and would win a pure smallest-fit;
    # the near-tie must resolve to the table order (ISO first).
    decisions = choose_crops([_page(1, (5, 0, 208, 270))], _DPI, flatbed=False)

    assert decisions[0].label == "a4"
    assert decisions[0].box_px is not None
    x0, y0, x1, y1 = decisions[0].box_px
    assert (y0, y1) == (0, _mm(297))  # top-anchored, exactly A4 high
    assert x1 - x0 == _mm(210)
    assert x0 <= _mm(5)  # the content stays inside the box


def test_wide_content_needs_letter() -> None:
    # 213 mm of content cannot be A4 paper (210 mm wide).
    decisions = choose_crops([_page(1, (1, 0, 214, 260))], _DPI, flatbed=False)

    assert decisions[0].label == "letter"


def test_small_content_snaps_to_the_small_size() -> None:
    decisions = choose_crops([_page(1, (60, 0, 130, 90))], _DPI, flatbed=False)

    assert decisions[0].label == "a6"


def test_sparse_wide_page_adopts_the_batch_majority() -> None:
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (5, 0, 205, 250)),
        # Sparse but full-width lines: plausibly the same A4 paper.
        _page(3, (20, 0, 190, 60)),
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == ["a4", "a4", "a4"]
    boxes = [decision.box_px for decision in decisions]
    heights = {box[3] - box[1] for box in boxes if box is not None}
    assert len(boxes) == 3 and None not in boxes
    assert heights == {_mm(297)}


def test_narrow_content_does_not_adopt_the_majority() -> None:
    # 70 mm wide content between A4 pages is smaller paper (a receipt, an
    # A6 card), not a sparse A4 page; the majority must not inflate it.
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (5, 0, 205, 250)),
        _page(3, (60, 40, 130, 130)),
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == ["a4", "a4", "a6"]


def test_dense_smaller_paper_keeps_its_own_size() -> None:
    # A fully printed A5 sheet between two A4 sheets stays A5.
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (10, 5, 140, 200)),
        _page(3, (5, 0, 205, 270)),
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == ["a4", "a5", "a4"]


def test_even_split_has_no_majority_and_keeps_both_sizes() -> None:
    # 50/50 A4/legal: a plurality must not rewrite half the batch.
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (2, 0, 210, 340)),
        _page(3, (5, 0, 205, 270)),
        _page(4, (2, 0, 210, 340)),
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == [
        "a4",
        "legal",
        "a4",
        "legal",
    ]


def test_duplex_pairs_share_the_sheet_size() -> None:
    # Page 2 is the back of page 1's sheet: same physical paper, so the
    # sparse narrow back side comes out A4 with its front, not A6.
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (60, 10, 130, 100)),
        _page(3, (5, 0, 205, 260)),
        _page(4, (5, 0, 205, 250)),
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False, duplex=True)

    assert [decision.label for decision in decisions] == ["a4", "a4", "a4", "a4"]


def test_feeder_containment_uses_the_leading_edge_distance() -> None:
    # Content from 20 to 310 mm spans only 290 mm, but sits 310 mm from the
    # leading edge where the crop anchors: A4 (297 mm) would cut 13 mm of
    # detected text, so the sheet must come out legal.
    decisions = choose_crops([_page(1, (10, 20, 200, 310))], _DPI, flatbed=False)

    assert decisions[0].label == "legal"
    assert decisions[0].box_px is not None
    assert decisions[0].box_px[3] >= _mm(310)  # all content inside


def test_crop_expands_over_the_permissive_reach() -> None:
    # A faint page number at 320 mm is no sizing evidence (not in the robust
    # box) but must never be cut: the placed box grows to include the reach.
    page = PageContent(
        number=1,
        path=Path("page_0001.pnm"),
        frame_px=_FRAME,
        bbox_px=(_mm(5), 0, _mm(205), _mm(270)),
        reach_px=(_mm(5), 0, _mm(205), _mm(322)),
    )

    decisions = choose_crops([page], _DPI, flatbed=False)

    assert decisions[0].label == "a4"
    assert decisions[0].box_px is not None
    assert decisions[0].box_px[3] == _mm(322)  # grown past 297 mm, no cut


def test_kept_blank_page_adopts_the_batch_majority() -> None:
    pages = [_page(1, (5, 0, 205, 270)), _page(2, None)]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == ["a4", "a4"]
    assert decisions[1].box_px is not None


def test_all_blank_batch_keeps_the_frames() -> None:
    decisions = choose_crops([_page(1, None), _page(2, None)], _DPI, flatbed=False)

    assert all(decision.box_px is None for decision in decisions)
    assert all(decision.label == "frame" for decision in decisions)


def test_content_exceeding_the_majority_keeps_its_own_size() -> None:
    pages = [
        _page(1, (5, 0, 205, 270)),
        _page(2, (5, 0, 205, 260)),
        _page(3, (2, 0, 210, 340)),  # longer than A4: needs legal
    ]

    decisions = choose_crops(pages, _DPI, flatbed=False)

    assert [decision.label for decision in decisions] == ["a4", "a4", "legal"]


def test_receipt_shaped_content_skips_standard_sizes() -> None:
    # 60 x 180 mm content is receipt-shaped; snapping it to A5 would invent
    # 68 mm of white paper width. It gets its content box plus margins.
    decisions = choose_crops([_page(1, (20, 0, 80, 180))], _DPI, flatbed=False)

    assert decisions[0].label == "content"
    assert decisions[0].box_px is not None
    x0, _y0, x1, _y1 = decisions[0].box_px
    assert x1 - x0 == _mm(60) + 2 * _mm(10)  # content plus side margins


def test_overlong_content_gets_a_free_size_with_margins() -> None:
    # 365 mm of content: no standard size fits; content plus margins.
    decisions = choose_crops([_page(1, (60, 0, 140, 365))], _DPI, flatbed=False)

    assert decisions[0].label == "content"
    assert decisions[0].box_px is not None
    x0, y0, x1, y1 = decisions[0].box_px
    assert y0 == 0
    assert y1 == _mm(365) + _mm(15)  # tail margin below the last content
    assert x0 == _mm(60) - _mm(10)  # side margins around the content
    assert x1 == _mm(140) + _mm(10)


def test_flatbed_places_the_box_around_the_content() -> None:
    # A6 original lying somewhere mid-glass: the crop centers on it instead
    # of anchoring to the frame top.
    decisions = choose_crops([_page(1, (30, 120, 120, 240))], _DPI, flatbed=True)

    assert decisions[0].label == "a6"
    assert decisions[0].box_px is not None
    x0, y0, x1, y1 = decisions[0].box_px
    assert y0 > 0
    assert x0 <= _mm(30) and x1 >= _mm(120)  # content inside
    assert y0 <= _mm(120) and y1 >= _mm(240)


def test_full_frame_content_degenerates_to_a_no_op() -> None:
    # Ink everywhere (dark backing, test pattern): nothing contains the box,
    # the free size clamps to the frame, and the decision is "leave it".
    frame_w_mm = _FRAME[0] / _SCALE
    frame_h_mm = _FRAME[1] / _SCALE
    decisions = choose_crops(
        [_page(1, (0, 0, frame_w_mm, frame_h_mm))], _DPI, flatbed=False
    )

    assert decisions[0].box_px is None
