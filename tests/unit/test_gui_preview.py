"""Tests for the advisory output-filename preview (GTK-free, no hardware).

The preview only looks; the CLI's exclusive-create reservation at scan
start stays the authoritative choice of an output name.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from scanmole.naming import output_candidates
from scanmole_gui.preview import (
    CANDIDATE_LIMIT,
    PreviewOutcome,
    first_free_candidate,
)

WHEN = datetime(2026, 8, 23, 14, 5, 9)


def _look(
    folder: Path,
    template: str = "scan_{NNN}.pdf",
    *,
    stat: Callable[[Path], os.stat_result] = os.lstat,
    limit: int = CANDIDATE_LIMIT,
) -> PreviewOutcome:
    """One advisory look at ``folder`` for ``template``."""
    candidates = output_candidates(str(folder / template), when=WHEN, device=None)
    return first_free_candidate(candidates, stat=stat, limit=limit)


def test_the_preview_skips_names_that_are_already_taken(tmp_path: Path) -> None:
    (tmp_path / "scan_001.pdf").touch()
    (tmp_path / "scan_002.pdf").touch()

    outcome = _look(tmp_path)

    assert outcome.path == tmp_path / "scan_003.pdf"
    assert outcome.available is True


def test_the_preview_creates_nothing(tmp_path: Path) -> None:
    before = sorted(tmp_path.iterdir())

    assert _look(tmp_path).path == tmp_path / "scan_001.pdf"

    assert sorted(tmp_path.iterdir()) == before  # no reservation, no directory


def test_a_dangling_symlink_occupies_its_candidate(tmp_path: Path) -> None:
    # A link standing where the output would go is a taken name, not an
    # instruction to write somewhere else: both sides skip it and neither
    # the link nor its absent target is touched.
    from scanmole.cli import _resolve_output

    link = tmp_path / "scan_001.pdf"
    target = tmp_path / "elsewhere.pdf"
    link.symlink_to(target)

    previewed = _look(tmp_path)
    args = type(
        "Args", (), {"output": str(tmp_path / "scan_{NNN}.pdf"), "outbase": None}
    )()
    reserved = _resolve_output(args, None)

    assert previewed.path == reserved == tmp_path / "scan_002.pdf"
    assert link.is_symlink() and link.readlink() == target
    assert not target.exists()


def test_a_final_symlink_cannot_redirect_output_out_of_the_folder(
    tmp_path: Path,
) -> None:
    from scanmole.cli import _resolve_output

    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "scan_001.pdf").symlink_to(tmp_path / "outside.pdf")

    previewed = _look(chosen)
    args = type(
        "Args", (), {"output": str(chosen / "scan_{NNN}.pdf"), "outbase": None}
    )()
    reserved = _resolve_output(args, None)

    assert previewed.path == reserved == chosen / "scan_002.pdf"
    assert reserved.parent == chosen  # never the symlink's target directory
    assert not (tmp_path / "outside.pdf").exists()


def test_a_symlinked_output_directory_is_still_written_through(
    tmp_path: Path,
) -> None:
    # Only the final component keeps its identity; a directory symlink,
    # including one used as the selected folder, resolves as before.
    from scanmole.cli import _resolve_output

    real = tmp_path / "real"
    real.mkdir()
    (real / "nested").mkdir()
    link = tmp_path / "via-link"
    link.symlink_to(real)

    args = type(
        "Args", (), {"output": str(link / "nested" / "scan_{NN}.pdf"), "outbase": None}
    )()
    reserved = _resolve_output(args, None)

    assert reserved == real / "nested" / "scan_01.pdf"
    assert reserved.exists()
    assert (
        _look(real / "nested", "scan_{NN}.pdf").path == real / "nested" / "scan_02.pdf"
    )


def test_a_candidate_is_inspected_without_following_links(tmp_path: Path) -> None:
    # os.lstat, not os.path.exists: exclusive creation fails on a link
    # whose target is gone, so a name that only exists as such a link is
    # taken, never free.
    dangling = {tmp_path / "scan_001.pdf"}

    def like_lstat(path: Path) -> os.stat_result:
        if path in dangling:
            return os.lstat(tmp_path)  # the link itself is there
        if path == tmp_path:
            return os.lstat(tmp_path)
        raise FileNotFoundError(path)

    assert _look(tmp_path, stat=like_lstat).path == tmp_path / "scan_002.pdf"


def test_a_missing_directory_is_unavailable_not_free(tmp_path: Path) -> None:
    outcome = _look(tmp_path / "absent")

    assert outcome.available is False
    assert outcome.path is None
    assert outcome.reason == "missing-directory"


def test_a_parent_that_is_not_a_directory_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("not a directory")

    outcome = _look(tmp_path / "file")

    assert outcome.available is False
    assert outcome.reason == "not-a-directory"


def test_a_vanished_parent_is_never_read_as_an_available_name(
    tmp_path: Path,
) -> None:
    # The candidate's own FileNotFoundError says nothing on its own: it
    # means "free" only while the parent is still a directory.
    gone = tmp_path / "vanishing"
    gone.mkdir()
    seen: list[Path] = []

    def stat(path: Path) -> os.stat_result:
        seen.append(path)
        if path == gone and len(seen) > 1:
            raise FileNotFoundError(path)
        if path.name.startswith("scan_"):
            raise FileNotFoundError(path)
        return os.lstat(path)

    outcome = _look(gone, stat=stat)

    assert outcome.available is False
    assert outcome.reason == "missing-directory"


def test_an_inspection_failure_is_reported_rather_than_raised(
    tmp_path: Path,
) -> None:
    def denied(path: Path) -> os.stat_result:
        if path == tmp_path:
            return os.lstat(path)
        raise PermissionError(path)

    outcome = _look(tmp_path, stat=denied)

    assert outcome.available is False
    assert outcome.reason == "unreadable"

    def broken(path: Path) -> os.stat_result:
        raise OSError("injected")

    assert _look(tmp_path, stat=broken).reason == "unreadable"


def test_the_preview_stops_at_its_bound_without_claiming_more(
    tmp_path: Path,
) -> None:
    # Reaching the bound says the preview has no answer. Whether a free
    # name exists further along is the reservation's business.
    seen: list[Path] = []

    def taken(path: Path) -> os.stat_result:
        seen.append(path)
        if path == tmp_path:
            return os.lstat(path)
        return os.lstat(__file__)  # every candidate exists

    outcome = _look(tmp_path, stat=taken, limit=5)

    assert outcome.available is False
    assert outcome.reason == "search-limit"
    # Exactly the bound was inspected, and nothing past it.
    candidates = [path for path in seen if path.name.startswith("scan_")]
    assert [path.name for path in candidates] == [
        f"scan_{index:03d}.pdf" for index in range(1, 6)
    ]


def test_the_cli_reserves_past_the_previews_bound(tmp_path: Path) -> None:
    from scanmole.cli import _resolve_output
    from scanmole_gui.preview import CANDIDATE_LIMIT

    for index in range(1, CANDIDATE_LIMIT + 1):
        (tmp_path / f"scan_{index:03d}.pdf").touch()

    assert _look(tmp_path).reason == "search-limit"  # the preview gives up

    args = type(
        "Args", (), {"output": str(tmp_path / "scan_{NNN}.pdf"), "outbase": None}
    )()
    assert _resolve_output(args, None).name == f"scan_{CANDIDATE_LIMIT + 1:03d}.pdf"


def test_the_preview_and_the_reservation_walk_the_same_sequence(
    tmp_path: Path,
) -> None:
    # Whatever the preview points at, the reservation must land on unless
    # something else takes it in between.
    from scanmole.cli import _resolve_output

    template = str(tmp_path / "doc_{NN}.pdf")
    (tmp_path / "doc_01.pdf").touch()
    previewed = _look(tmp_path, "doc_{NN}.pdf")

    args = type("Args", (), {"output": template, "outbase": None})()
    reserved = _resolve_output(args, None)

    assert previewed.path == reserved == tmp_path / "doc_02.pdf"
    assert reserved.stat().st_size == 0  # reserved, not written
