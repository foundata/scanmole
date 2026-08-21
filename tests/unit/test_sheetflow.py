"""Tests for the sheet-flow bookkeeping: segment identity and numbering."""

from __future__ import annotations

from pathlib import Path

from scanmole.sheetflow import PageOrigin, count_sheets, next_page_number


def test_sheet_key_pairs_duplex_frames_within_a_segment() -> None:
    assert PageOrigin(segment=1, frame=1).sheet_key(duplex=True) == (1, 1)
    assert PageOrigin(segment=1, frame=2).sheet_key(duplex=True) == (1, 1)
    assert PageOrigin(segment=1, frame=3).sheet_key(duplex=True) == (1, 2)


def test_sheet_key_never_pairs_across_segments() -> None:
    # An odd final frame is an incomplete sheet; the next segment's first
    # frame starts a new physical sheet even though the global numbering
    # continues without a gap.
    last_of_first = PageOrigin(segment=1, frame=3).sheet_key(duplex=True)
    first_of_second = PageOrigin(segment=2, frame=1).sheet_key(duplex=True)

    assert last_of_first != first_of_second


def test_sheet_key_counts_each_simplex_frame_as_a_sheet() -> None:
    assert PageOrigin(segment=2, frame=3).sheet_key(duplex=False) == (2, 3)


def test_count_sheets_simplex_counts_frames() -> None:
    assert count_sheets([2, 3], duplex=False) == 5


def test_count_sheets_duplex_pairs_per_segment() -> None:
    # 3 frames in one segment are two physical sheets (one incomplete);
    # a global pairing over 3+2 frames would report wrongly.
    assert count_sheets([3, 2], duplex=True) == 3
    assert count_sheets([2, 2], duplex=True) == 2
    assert count_sheets([], duplex=True) == 0


def test_next_page_number_starts_at_one(tmp_path: Path) -> None:
    assert next_page_number(tmp_path) == 1


def test_next_page_number_follows_the_greatest_artifact(tmp_path: Path) -> None:
    # The greatest existing file decides, never the page-event count: an
    # unannounced or partial frame on disk must not be overwritten by the
    # next segment.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    (tmp_path / "page_0003.pnm").write_bytes(b"partial")
    (tmp_path / "unrelated.txt").write_text("x")

    assert next_page_number(tmp_path) == 4
