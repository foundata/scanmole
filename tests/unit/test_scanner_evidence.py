"""Tests for the scanner evidence kit (no hardware, fake scanimage)."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

KIT = Path(__file__).parent.parent.parent / "scripts" / "scanner-evidence"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolve deferred annotations
    # through sys.modules at class-creation time.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pnm_inventory = _load("pnm_inventory")
print_pack = _load("print_pack")


# ------------------------------------------------------- pnm_inventory.py


def test_inventory_parses_all_binary_formats(tmp_path: Path) -> None:
    p4 = tmp_path / "a.pbm"
    p4.write_bytes(b"P4\n10 2\n" + bytes(4))  # odd width: 2 bytes per row
    p5 = tmp_path / "b.pgm"
    p5.write_bytes(b"P5\n# a comment\n3 2\n255\n" + bytes(6))
    deep = tmp_path / "c.ppm"
    deep.write_bytes(b"P6\n2 1\n65535\n" + bytes(12))  # 16-bit samples

    rows = [pnm_inventory.inventory_row(path) for path in (p4, p5, deep)]

    assert rows[0][1:5] == ("P4", "10", "2", "-")
    assert rows[0][6] == "4"  # ((10+7)//8) * 2
    assert rows[1][1:5] == ("P5", "3", "2", "255")
    assert rows[2][1:5] == ("P6", "2", "1", "65535")
    assert rows[2][6] == "12"  # 2*1*3 samples, 2 bytes each
    assert all(row[7] == "0" for row in rows)  # no trailing bytes
    assert all(len(row[8]) == 64 for row in rows)  # sha256 hex


def test_inventory_reports_trailing_bytes_without_rejecting(tmp_path: Path) -> None:
    # The ScanSnap iX500 occasionally delivers one raster row beyond the
    # declared height; the inventory exposes it instead of reinterpreting.
    page = tmp_path / "extra.pgm"
    page.write_bytes(b"P5\n4 2\n255\n" + bytes(8) + bytes(4))

    row = pnm_inventory.inventory_row(page)

    assert row[6] == "8"  # expected raster
    assert row[7] == "4"  # one extra row, reported


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"P1\n2 2\n0 1 1 0\n", "magic"),
        (b"P5\n0 2\n255\n", "dimensions"),
        (b"P5\n2 -1\n255\n" + bytes(4), "dimensions"),
        (b"P5\nx 2\n255\n" + bytes(4), "dimensions"),
        (b"P5\n2 2\n0\n" + bytes(4), "maxval"),
        (b"P5\n2 2\n70000\n" + bytes(4), "maxval"),
        (b"P5\n2 2\n255\n" + bytes(3), "truncated raster"),
        (b"P5\n2 2", "truncated"),
    ],
)
def test_inventory_rejects_malformed_files(
    tmp_path: Path, data: bytes, message: str
) -> None:
    page = tmp_path / "bad.pnm"
    page.write_bytes(data)

    with pytest.raises(pnm_inventory.PnmError, match=message):
        pnm_inventory.inventory_row(page)


def test_inventory_main_continues_past_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.pgm"
    good.write_bytes(b"P5\n2 1\n255\n" + bytes(2))
    bad = tmp_path / "bad.pgm"
    bad.write_bytes(b"P5\n2 1\n255\n")

    assert pnm_inventory.main([str(good), str(bad)]) == 1

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].startswith("file\t")
    assert lines[1].startswith("good.pgm\t")  # the valid row still landed
    assert "bad.pgm" in captured.err
    assert pnm_inventory.main([]) == 2


# ---------------------------------------------------------- print_pack.py


def test_print_pack_renders_deterministically() -> None:
    first = print_pack.render_pack()

    assert first == print_pack.render_pack()
    assert f"%%Pages: {print_pack.PAGE_COUNT}" in first
    assert first.count("%%Page:") == print_pack.PAGE_COUNT
    assert first.count("showpage") == print_pack.PAGE_COUNT


def test_print_pack_matches_the_committed_file() -> None:
    committed = (KIT / "print-pack.ps").read_text(encoding="utf-8")

    assert committed == print_pack.render_pack()
    assert print_pack.main(["--check"]) == 0


def test_print_pack_check_fails_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "print-pack.ps"
    stale.write_text("%!PS-Adobe-3.0\n%%EOF\n", encoding="utf-8")
    monkeypatch.setattr(print_pack, "pack_path", lambda: stale)

    assert print_pack.main(["--check"]) == 1
    assert "does not match" in capsys.readouterr().err
    assert print_pack.main(["--bogus"]) == 2


def test_print_pack_labels_faces_and_keeps_blanks_unprinted() -> None:
    rendered = print_pack.render_pack()

    for label in (
        "D1 FRONT",
        "D6 BACK",
        "S1 SINGLE-SIDED",
        "S4 SINGLE-SIDED",
        "F1 FOOTER SHEET",
        "P1 sparse sheet",
        "U1 PUNCH SHEET",
        "U2 PUNCH SHEET",
        "TOP EDGE",
        "A1 A5 SHEET",
    ):
        assert label in rendered
    assert "KEEP THE BACK FACTORY BLANK" in rendered
    assert "(17)" in rendered  # the page-number sheet prints only "17"
    # Blank sheets are never printed: every page in the document carries
    # visible marks, and the plan says so explicitly.
    assert "never" in rendered and "blank" in rendered
    assert "F1-FOOTER-8MM" in rendered


# -------------------------------------------------------------- capture.sh

_FAKE_SCANIMAGE = """#!/usr/bin/env bash
set -u
if [ "${1:-}" = "--version" ]; then
  echo "fake-scanimage 9.9.9"
  exit 0
fi
printf '%s\\n' "$@" > "${FAKE_LOG}"
if [ -n "${FAKE_ASSERT_METADATA:-}" ] && [ ! -f "${FAKE_ASSERT_METADATA}" ]; then
  echo "metadata was not written before acquisition" >&2
  exit 99
fi
batch=""
for argument in "$@"; do
  case "${argument}" in --batch=*) batch="${argument#--batch=}" ;; esac
done
i=1
while [ "${i}" -le "${FAKE_PAGES:-2}" ]; do
  # shellcheck disable=SC2059 -- the batch pattern carries the %04d
  page="$(printf "${batch}" "${i}")"
  if [ "${i}" -eq "${FAKE_PAGES:-2}" ] && [ -n "${FAKE_TRUNCATE:-}" ]; then
    printf 'P5\\n2 2\\n255\\nAB' > "${page}"
  elif [ "${i}" -eq "${FAKE_PAGES:-2}" ] && [ -n "${FAKE_TRAILING:-}" ]; then
    printf 'P5\\n2 2\\n255\\nABCDwxyz' > "${page}"
  else
    printf 'P5\\n2 2\\n255\\nABCD' > "${page}"
  fi
  echo "${page}"
  i=$((i + 1))
done
echo "fake stderr noise" >&2
exit "${FAKE_EXIT:-0}"
"""


def _run_capture(
    tmp_path: Path,
    *extra: str,
    root: Path | None = None,
    run_id: str = "01-test-run",
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "scanimage"
    fake.write_text(_FAKE_SCANIMAGE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    out_root = root if root is not None else tmp_path / "evidence"
    run_dir = out_root / "test-device" / "runs" / run_id
    merged = dict(os.environ)
    merged["PATH"] = f"{bin_dir}:{merged['PATH']}"
    merged["FAKE_LOG"] = str(tmp_path / "fake-argv.log")
    merged.update(env or {})
    result = subprocess.run(
        [
            str(KIT / "capture.sh"),
            "--output-root",
            str(out_root),
            "--device-label",
            "test-device",
            "--device",
            "fake:backend:0",
            "--run",
            run_id,
            "--source",
            "ADF Duplex",
            "--mode",
            "Gray",
            "--resolution",
            "300",
            "--paper",
            "synthetic",
            "--orientation",
            "normal",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=merged,
        check=False,
    )
    return result, run_dir


def test_capture_success_records_everything(tmp_path: Path) -> None:
    result, run_dir = _run_capture(
        tmp_path, "--", "--page-width", "221.121", "--ald=yes"
    )

    assert result.returncode == 0, result.stderr
    metadata = (run_dir / "metadata.txt").read_text()
    assert "status: completed (scanimage exit 0, 2 frame(s))" in metadata
    assert "scanimage: fake-scanimage 9.9.9" in metadata
    assert "device: fake:backend:0" in metadata
    assert "--ald=yes" in metadata  # the shell-quoted argv is recorded
    assert "paper: synthetic" in metadata
    assert "orientation: normal" in metadata
    assert (run_dir / "scanimage-stdout.txt").read_text().count("page_") == 2
    assert "fake stderr noise" in (run_dir / "scanimage-stderr.txt").read_text()
    inventory = (run_dir / "inventory.tsv").read_text().splitlines()
    assert len(inventory) == 3  # header plus two pages
    assert inventory[1].startswith("page_0001.pnm\tP5\t2\t2\t255")


def test_capture_forwards_argv_exactly_without_shell_evaluation(
    tmp_path: Path,
) -> None:
    tricky = "value with spaces; $(touch /tmp/pwned) `id` *"
    result, _ = _run_capture(tmp_path, "--", "-x", tricky, "--ald=no")

    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "fake-argv.log").read_text().splitlines()
    resolution = argv.index("--resolution")
    assert argv[resolution + 2 : resolution + 5] == ["-x", tricky, "--ald=no"]
    assert argv[resolution + 5] == "--format=pnm"  # owned options follow


@pytest.mark.parametrize(
    "argument",
    [
        "-d",
        "-dother:0",
        "--device-name=other:0",
        "--source",
        "--source=ADF",
        "--mode=Color",
        "--resolution=600",
        "--format=tiff",
        "-b",
        "--batch=/x/%d.pnm",
        "--batch-print",
        "--batch-count=1",
        "-o",
        "--output-file=/tmp/x.pnm",
        "-L",
    ],
)
def test_capture_rejects_owned_option_overrides(tmp_path: Path, argument: str) -> None:
    result, run_dir = _run_capture(tmp_path, "--", argument, "value")

    assert result.returncode == 2
    assert "forwarded" in result.stderr
    assert not (tmp_path / "fake-argv.log").exists()  # scanimage never ran
    assert not run_dir.exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--device-label", "Bad Label"], "device label"),
        (["--run", "Bad/Run"], "run id"),
        (["--resolution", "abc"], "resolution"),
        (["--resolution", "0"], "resolution"),
    ],
)
def test_capture_validates_inputs_before_scanning(
    tmp_path: Path, options: list[str], message: str
) -> None:
    result, _ = _run_capture(tmp_path, *options)

    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "fake-argv.log").exists()


def test_capture_requires_every_option(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(KIT / "capture.sh"), "--output-root", str(tmp_path / "e")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "missing required option" in result.stderr


def test_capture_refuses_to_overwrite_a_run(tmp_path: Path) -> None:
    _run_capture(tmp_path)
    result, run_dir = _run_capture(tmp_path)

    assert result.returncode == 1
    assert "never overwritten" in result.stderr
    assert (run_dir / "metadata.txt").exists()  # the first run is untouched


def test_capture_refuses_output_inside_the_worktree(tmp_path: Path) -> None:
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=KIT,
            check=True,
        ).stdout.strip()
    )
    result, _ = _run_capture(tmp_path, root=repo_root / "dist" / "evidence")

    assert result.returncode == 2
    assert "Git worktree" in result.stderr
    assert not (repo_root / "dist" / "evidence").exists()


def test_capture_refuses_worktree_output_through_a_symlink(tmp_path: Path) -> None:
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=KIT,
            check=True,
        ).stdout.strip()
    )
    sneaky = tmp_path / "innocent-looking"
    sneaky.symlink_to(repo_root)
    result, _ = _run_capture(tmp_path, root=sneaky / "evidence")

    assert result.returncode == 2
    assert "Git worktree" in result.stderr


def test_capture_writes_metadata_before_acquisition(tmp_path: Path) -> None:
    run_dir = tmp_path / "evidence" / "test-device" / "runs" / "01-test-run"
    result, _ = _run_capture(
        tmp_path, env={"FAKE_ASSERT_METADATA": str(run_dir / "metadata.txt")}
    )

    # The fake exits 99 if the metadata file is missing at invocation time.
    assert result.returncode == 0, result.stderr


def test_capture_records_failure_and_preserves_partial_pages(
    tmp_path: Path,
) -> None:
    result, run_dir = _run_capture(tmp_path, env={"FAKE_EXIT": "5", "FAKE_PAGES": "1"})

    assert result.returncode == 1
    metadata = (run_dir / "metadata.txt").read_text()
    assert "status: FAILED (scanimage exit 5, 1 frame(s))" in metadata
    assert (run_dir / "page_0001.pnm").exists()  # preserved for analysis
    inventory = (run_dir / "inventory.tsv").read_text().splitlines()
    assert len(inventory) == 2  # header plus the completed page


def test_capture_treats_feeder_empty_exit_as_success(tmp_path: Path) -> None:
    result, run_dir = _run_capture(tmp_path, env={"FAKE_EXIT": "7", "FAKE_PAGES": "3"})

    assert result.returncode == 0, result.stderr
    assert (
        "status: completed (scanimage exit 7, 3 frame(s))"
        in (run_dir / "metadata.txt").read_text()
    )


def test_capture_marks_a_frameless_success_incomplete(tmp_path: Path) -> None:
    # A zero-exit scan that delivered nothing is not usable evidence: it
    # must not read as a completed run in a later corpus sweep.
    result, run_dir = _run_capture(tmp_path, env={"FAKE_PAGES": "0"})

    assert result.returncode == 1
    assert (
        "status: INCOMPLETE (scanimage exit 0, 0 frame(s), nothing delivered)"
        in (run_dir / "metadata.txt").read_text()
    )
    assert "INCOMPLETE" in result.stderr


def _victim_worktree(tmp_path: Path) -> Path:
    victim = tmp_path / "victim-repo"
    victim.mkdir()
    subprocess.run(["git", "init", "-q", str(victim)], check=True, capture_output=True)
    return victim


def test_capture_refuses_a_descendant_symlink_into_a_worktree(
    tmp_path: Path,
) -> None:
    # The output root itself is safe, but a pre-existing device-label
    # symlink points into a Git worktree: the canonicalized run directory
    # must be validated, not only the root.
    victim = _victim_worktree(tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "test-device").symlink_to(victim)

    result, _ = _run_capture(tmp_path, root=root)

    assert result.returncode == 2
    assert "Git worktree" in result.stderr
    assert list(victim.iterdir()) == [victim / ".git"]  # nothing written


def test_capture_follows_a_harmless_symlink_consistently(tmp_path: Path) -> None:
    # A device-label symlink to an ordinary non-Git directory is fine;
    # the run lands at the resolved location.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "test-device").symlink_to(elsewhere)

    result, _ = _run_capture(tmp_path, root=root)

    assert result.returncode == 0, result.stderr
    assert (elsewhere / "runs" / "01-test-run" / "metadata.txt").exists()


def test_truncated_frame_marks_the_run_incomplete(tmp_path: Path) -> None:
    # scanimage exits zero but the last frame is truncated: the run is
    # not trustworthy evidence and must say so, preserving everything.
    result, run_dir = _run_capture(tmp_path, env={"FAKE_TRUNCATE": "1"})

    assert result.returncode != 0
    metadata = (run_dir / "metadata.txt").read_text()
    assert "status: INCOMPLETE (scanimage exit 0, 2 frame(s)" in metadata
    assert (run_dir / "page_0001.pnm").exists()
    assert (run_dir / "page_0002.pnm").exists()  # the bad frame is preserved
    inventory = (run_dir / "inventory.tsv").read_text().splitlines()
    assert len(inventory) == 2  # header plus the one valid page
    assert "page_0002.pnm" in (run_dir / "inventory-errors.txt").read_text()


def test_trailing_bytes_do_not_mark_the_run_incomplete(tmp_path: Path) -> None:
    # One extra raster row beyond the declared height is a known valid
    # condition; it is reported, never treated as corruption.
    result, run_dir = _run_capture(tmp_path, env={"FAKE_TRAILING": "1"})

    assert result.returncode == 0, result.stderr
    assert "status: completed" in (run_dir / "metadata.txt").read_text()
    rows = (run_dir / "inventory.tsv").read_text().splitlines()
    assert rows[2].split("\t")[7] == "4"  # the trailing count is exposed


def test_failed_scan_with_truncated_page_keeps_both_facts(tmp_path: Path) -> None:
    result, run_dir = _run_capture(
        tmp_path, env={"FAKE_EXIT": "5", "FAKE_TRUNCATE": "1", "FAKE_PAGES": "1"}
    )

    assert result.returncode == 1
    metadata = (run_dir / "metadata.txt").read_text()
    assert (
        "status: FAILED (scanimage exit 5, 1 frame(s); inventory invalid)" in metadata
    )


@pytest.mark.parametrize(
    "option",
    [
        "--output-root",
        "--device-label",
        "--device",
        "--run",
        "--source",
        "--mode",
        "--resolution",
        "--paper",
        "--orientation",
    ],
)
def test_capture_reports_a_missing_option_value(tmp_path: Path, option: str) -> None:
    result = subprocess.run(
        [str(KIT / "capture.sh"), option],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert option in result.stderr
    assert "value" in result.stderr


@pytest.mark.parametrize(
    "argument",
    [
        "--dont-scan",
        "-n",
        "--test",
        "-T",
        "--formatted-device-list",
        "-f",
        "--batch-prompt",
    ],
)
def test_capture_rejects_operation_changing_globals(
    tmp_path: Path, argument: str
) -> None:
    result, run_dir = _run_capture(tmp_path, "--", argument)

    assert result.returncode == 2
    assert "forwarded" in result.stderr
    assert not (tmp_path / "fake-argv.log").exists()
    assert not run_dir.exists()


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--paper", "line one\nline two"),
        ("--device", "dev\rice"),
        ("--orientation", "tab\there"),
    ],
)
def test_capture_rejects_control_characters_in_metadata(
    tmp_path: Path, option: str, value: str
) -> None:
    result, _ = _run_capture(tmp_path, option, value)

    assert result.returncode == 2
    assert (
        "carriage" in result.stderr
        or "newline" in result.stderr
        or "control" in result.stderr
    )
    assert not (tmp_path / "fake-argv.log").exists()
