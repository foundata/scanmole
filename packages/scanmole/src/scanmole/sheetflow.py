"""Sheet-flow bookkeeping: which physical sheet every scanned frame belongs to.

A collect run invokes the scanner once per feeder reload (one *acquisition
segment*) while the page files keep one continuous numbering. Global page
numbers therefore cannot say where a physical sheet starts: a duplex segment
that ended on an odd frame left an incomplete sheet, and the next segment's
first frame starts a new sheet regardless of the numbering. The segment and
the frame's position within it travel with every delivered page instead of
being re-derived later.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_PAGE_FILE = re.compile(r"page_(\d+)\.pnm")


@dataclass(frozen=True)
class PageOrigin:
    """Where a delivered frame came from, both indices 1-based."""

    segment: int
    """The acquisition segment (scanner invocation) that produced the frame."""
    frame: int
    """The frame's position within its segment."""

    def sheet_key(self, duplex: bool) -> tuple[int, int]:
        """Group key of the physical sheet this frame belongs to.

        On a duplex source frames 1 and 2 of a segment are one sheet,
        frames 3 and 4 the next; elsewhere every frame is its own sheet.
        The segment is part of the key, so an odd final frame can never
        pair with the first frame of the following segment.
        """
        return (self.segment, (self.frame + 1) // 2 if duplex else self.frame)


def count_sheets(segment_frames: Sequence[int], duplex: bool) -> int:
    """Physical sheets represented by per-segment frame counts.

    A duplex segment with an odd frame count still counts its incomplete
    final sheet: the paper went through the scanner.
    """
    if not duplex:
        return sum(segment_frames)
    return sum((frames + 1) // 2 for frames in segment_frames)


def page_file_number(path: Path) -> int | None:
    """The number in a ``page_NNNN.pnm`` artifact name, or ``None``."""
    match = _PAGE_FILE.fullmatch(path.name)
    return int(match.group(1)) if match else None


def next_page_number(work_dir: Path) -> int:
    """1 + the greatest existing page artifact number (1 when none exist).

    Numbering scans the directory rather than counting page events, so an
    unannounced or partial frame on disk is never overwritten by the next
    segment.
    """
    greatest = 0
    for path in work_dir.iterdir():
        number = page_file_number(path)
        if number is not None:
            greatest = max(greatest, number)
    return greatest + 1
