"""Tests for the geometric skew oracle, on synthesized frames.

The oracle exists to grade a backend deskew mechanism without asking the
tool ScanMole deskews with, so its own failure modes have to be pinned
rather than trusted. Two of them are already known from real hardware: a
scanner's trailing-edge shadow spans the full width and is perfectly
straight, and a genuine R1 frame can lose a rule at high skew.

Frames here are drawn, not captured, so every angle is known exactly and
the artifacts can be placed where they do the most damage.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

KIT = Path(__file__).parent.parent.parent / "scripts" / "scanner-evidence"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oracle = _load("skew_oracle")

DPI = 300
WIDTH = 2480
HEIGHT = 3500
SPACING = round(oracle.RULE_SPACING_MM * DPI / 25.4)
FIRST_RULE = 700
MARGIN = 200  # the printed rules stop short of the paper edge, as R1 does
THICKNESS = 4  # a printed hairline covers about this many rows at 300 dpi


def _blank() -> list[bytearray]:
    return [bytearray(WIDTH) for _ in range(HEIGHT)]


def _draw_rule(rows: list[bytearray], y: float, degrees: float = 0.0) -> None:
    """One full-width printed rule, tilted about the frame centre."""
    slope = math.tan(math.radians(degrees))
    for x in range(MARGIN, WIDTH - MARGIN):
        at = y + (x - WIDTH / 2) * slope
        for offset in range(THICKNESS):
            pixel = int(at) + offset
            if 0 <= pixel < HEIGHT:
                rows[pixel][x] = 1


def _full_width_line(rows: list[bytearray], y: int) -> None:
    """A perfectly straight artifact spanning the entire frame width."""
    if 0 <= y < HEIGHT:
        for x in range(WIDTH):
            rows[y][x] = 1


def _sheet(degrees: float = 0.0, rules: int = 3) -> list[bytearray]:
    rows = _blank()
    for index in range(rules):
        _draw_rule(rows, FIRST_RULE + index * SPACING, degrees)
    return rows


def _write(path: Path, rows: list[bytearray], kind: str = "P4") -> Path:
    """Persist drawn rows as a raw PNM of the requested family."""
    if kind == "P4":
        stride = (WIDTH + 7) // 8
        raster = bytearray()
        for row in rows:
            packed = bytearray(stride)
            for x, dark in enumerate(row):
                if dark:
                    packed[x // 8] |= 0x80 >> (x % 8)
            raster += packed
        path.write_bytes(b"P4\n%d %d\n" % (WIDTH, HEIGHT) + bytes(raster))
        return path
    if kind == "P5":
        raster = bytearray()
        for row in rows:
            raster += bytes(0 if dark else 255 for dark in row)
        path.write_bytes(b"P5\n%d %d\n255\n" % (WIDTH, HEIGHT) + bytes(raster))
        return path
    raster = bytearray()
    for row in rows:
        for dark in row:
            raster += b"\x00\x00\x00" if dark else b"\xff\xff\xff"
    path.write_bytes(b"P6\n%d %d\n255\n" % (WIDTH, HEIGHT) + bytes(raster))
    return path


def _measure(path: Path) -> Any:
    return oracle.measure(path, DPI, "t")


# --------------------------------------------------------- rule identity


@pytest.mark.parametrize("kind", ["P4", "P5", "P6"])
@pytest.mark.parametrize("degrees", [0.0, +0.5, -0.5, +1.5, -1.5])
def test_the_three_rules_are_found_and_measured(
    tmp_path: Path, kind: str, degrees: float
) -> None:
    # The whole point: a known rotation comes back as itself, in every
    # raster family the capture wrapper can write.
    result = _measure(_write(tmp_path / "p.pnm", _sheet(degrees), kind))

    assert result.rules == 3
    assert result.complete
    assert result.rigid == pytest.approx(degrees, abs=0.02)
    assert result.spread == pytest.approx(0.0, abs=0.02)


def test_a_final_row_shadow_is_not_a_printed_rule(tmp_path: Path) -> None:
    # The exact artifact the iX100 produced: a full-width dark band on
    # the very last raster row, perfectly straight, which read as a
    # flawless rule at zero degrees and pulled the verdict towards
    # "already straight".
    rows = _sheet(+1.0)
    _full_width_line(rows, HEIGHT - 1)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rules == 3
    assert result.rigid == pytest.approx(1.0, abs=0.02)


def test_a_leading_edge_shadow_is_not_a_printed_rule(tmp_path: Path) -> None:
    # The same artifact at the other end, which an exclusion written for
    # the trailing edge alone would have missed.
    rows = _sheet(+1.0)
    _full_width_line(rows, FIRST_RULE // 3)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rules == 3
    assert result.rigid == pytest.approx(1.0, abs=0.02)


def test_a_shadow_one_spacing_above_the_first_rule_is_ambiguous(
    tmp_path: Path,
) -> None:
    # Not a contrived case: R1's first rule sits 60 mm from the top edge,
    # exactly one printed spacing, so on a frame cropped to the paper a
    # leading-edge shadow lands where a fourth rule would. Two triplets
    # then fit equally well and neither can be preferred. Refusing is the
    # point: the alternative is the shadow silently deciding the angle.
    rows = _blank()
    top = SPACING + 200  # far enough down to leave room for the shadow
    for index in range(3):
        _draw_rule(rows, top + index * SPACING, +1.0)
    _full_width_line(rows, top - SPACING)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rigid is None
    assert "more than one" in result.note


def test_an_artifact_away_from_both_edges_is_not_a_printed_rule(
    tmp_path: Path,
) -> None:
    # A margin rule cannot help here at all: this one sits in the middle
    # of the page. Only the printed spacing separates it from a target.
    rows = _sheet(+1.0)
    _full_width_line(rows, FIRST_RULE + SPACING // 3)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rules == 3
    assert result.rigid == pytest.approx(1.0, abs=0.02)


def test_cropping_that_preserves_spacing_still_identifies_the_sheet(
    tmp_path: Path,
) -> None:
    # Automatic page size moves the rules' absolute positions on every
    # frame. Identity has to survive that, because their spacing does.
    rows = _sheet(+0.8)
    shifted = rows[300:] + [bytearray(WIDTH) for _ in range(300)]

    result = _measure(_write(tmp_path / "p.pnm", shifted))

    assert result.rules == 3
    assert result.rigid == pytest.approx(0.8, abs=0.02)


def test_a_missing_rule_is_reported_as_incomplete_not_substituted(
    tmp_path: Path,
) -> None:
    # A real frame can lose a rule at high skew. The pair still yields an
    # angle, and the count says the evidence is short: what must never
    # happen is a shadow quietly taking the third slot.
    rows = _sheet(+1.0, rules=2)
    _full_width_line(rows, HEIGHT - 1)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rules == 2
    assert not result.complete
    assert result.rigid == pytest.approx(1.0, abs=0.05)


def test_an_extra_full_width_line_at_the_printed_spacing_is_ambiguous(
    tmp_path: Path,
) -> None:
    # Four evenly spaced full-width lines offer two equally good
    # triplets. Picking one would be a guess, so the frame reports
    # nothing rather than a number nobody can defend.
    rows = _sheet(+1.0)
    _draw_rule(rows, FIRST_RULE + 3 * SPACING, +1.0)

    result = _measure(_write(tmp_path / "p.pnm", rows))

    assert result.rigid is None
    assert "more than one" in result.note


def test_a_frame_without_any_rules_reports_no_angle(tmp_path: Path) -> None:
    # A factory-blank duplex back. Correct, and not a pass.
    result = _measure(_write(tmp_path / "p.pnm", _blank()))

    assert result.rigid is None
    assert result.rules == 0


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"P4\n", id="truncated-header"),
        pytest.param(b"P4\n2480 3500\n\x00\x01", id="truncated-raster"),
        pytest.param(b"P7\n1 1\n255\n\x00", id="unsupported-family"),
        pytest.param(b"not a pnm at all", id="not-a-pnm"),
    ],
)
def test_malformed_input_is_reported_not_crashed(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "p.pnm"
    path.write_bytes(content)

    result = _measure(path)

    assert result.rigid is None
    assert result.note


# ------------------------------------------------------------ the gates


def _measurement(rigid: float, spread: float = 0.0, rules: int = 3) -> Any:
    return oracle.Measurement("m", rigid, spread, rules, (rigid,))


def _verdict(measurements: list[Any], expected: int = 1) -> int:
    return int(oracle.report(measurements, "group", expected))


@pytest.mark.parametrize(
    ("rigid", "passes"),
    [
        pytest.param(0.099, True, id="just-inside"),
        pytest.param(0.100, True, id="exactly-at"),
        pytest.param(0.101, False, id="just-outside"),
    ],
)
def test_the_ten_hundredths_threshold_is_inclusive(
    capsys: pytest.CaptureFixture[str], rigid: float, passes: bool
) -> None:
    # Mirrored so the signed median is zero and cannot decide the
    # outcome: what is under test is the share within 0.10 degrees.
    group = [_measurement(+rigid), _measurement(-rigid)]

    assert (_verdict(group, expected=2) == 0) is passes
    capsys.readouterr()


@pytest.mark.parametrize(
    ("worst", "passes"),
    [
        pytest.param(0.200, True, id="exactly-at"),
        pytest.param(0.201, False, id="just-outside"),
    ],
)
def test_the_ceiling_is_inclusive(
    capsys: pytest.CaptureFixture[str], worst: float, passes: bool
) -> None:
    # Twenty frames so one outlier stays inside the 95% allowance and
    # the ceiling is the only thing that can fail.
    group = [_measurement(0.01) for _ in range(19)] + [_measurement(worst)]

    assert (_verdict(group, expected=20) == 0) is passes
    capsys.readouterr()


@pytest.mark.parametrize(
    ("bias", "passes"),
    [
        pytest.param(0.050, True, id="exactly-at"),
        pytest.param(0.051, False, id="just-outside"),
    ],
)
def test_the_signed_median_threshold_is_inclusive(
    capsys: pytest.CaptureFixture[str], bias: float, passes: bool
) -> None:
    # A consistent lean every frame shares: individually within every
    # other limit, and exactly what the signed median exists to catch.
    group = [_measurement(bias) for _ in range(5)]

    assert (_verdict(group, expected=5) == 0) is passes
    capsys.readouterr()


def test_one_good_frame_cannot_carry_a_group_of_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The false pass this gate closes: if unmeasurable frames left the
    # denominator, a single clean result would report 100%.
    group = [_measurement(0.01)] + [
        oracle.Measurement(f"m{index}", None, None, 0, (), "unreadable")
        for index in range(99)
    ]

    assert _verdict(group, expected=100) == 1
    assert "[FAIL] every supplied frame measurable" in capsys.readouterr().out


def test_fewer_frames_than_expected_fails_the_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Five repetitions were required; three were supplied. Perfect
    # numbers over too few sheets are not the evidence that was asked
    # for.
    assert _verdict([_measurement(0.01) for _ in range(3)], expected=5) == 1
    assert "every expected frame supplied" in capsys.readouterr().out


def test_an_incomplete_triplet_fails_the_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A two-rule frame still has an angle, and still is not complete
    # evidence about a sheet that carries three.
    assert _verdict([_measurement(0.01, rules=2)], expected=1) == 1
    assert "[FAIL] every frame shows all 3 printed rules" in capsys.readouterr().out


def test_a_clean_pass_does_not_claim_qualification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The tool measures angles. Everything else the runbook demands is
    # judged elsewhere, and the output has to say so.
    assert _verdict([_measurement(0.01)]) == 0

    out = capsys.readouterr().out
    assert "qualification also needs" in out
    assert "sharpness" in out


def test_the_spread_is_reported_and_never_excuses_the_residual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A badly deformed sheet whose rotation was left in place. Mirrored
    # so the signed median is zero: what must fail is the residual
    # ceiling, which subtracting the spread would have turned into a
    # pass.
    group = [_measurement(+0.30, spread=0.50), _measurement(-0.30, spread=0.50)]

    assert _verdict(group, expected=2) == 1

    out = capsys.readouterr().out
    assert "[FAIL] no rigid residual above 0.20 deg" in out
    assert "non-rigid spread" in out
    assert "0.500" in out


# ------------------------------------------------------- what it prints


def test_no_path_reaches_stdout_or_the_tsv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A report has to be pastable into an issue without redaction, so
    # the frame's location must never travel with its measurement.
    frame = _write(tmp_path / "secret-scanner-42.pnm", _sheet(0.4))
    rows = tmp_path / "rows.tsv"

    oracle.main(["--label", "grp", "--expect", "1", "--tsv", str(rows), str(frame)])

    written = rows.read_text(encoding="utf-8")
    assert "secret-scanner-42" not in capsys.readouterr().out
    assert "secret-scanner-42" not in written and str(tmp_path) not in written
    assert "grp#01" in written


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--expect", "0"], id="zero-expected"),
        pytest.param(["--expect", "-1"], id="negative-expected"),
        pytest.param(["--expect", "1", "-r", "0"], id="zero-dpi"),
        pytest.param(["--expect", "1", "-r", "-300"], id="negative-dpi"),
    ],
)
def test_nonsensical_arguments_are_usage_errors(
    tmp_path: Path, argv: list[str]
) -> None:
    frame = _write(tmp_path / "p.pnm", _sheet())

    with pytest.raises(SystemExit) as info:
        oracle.main(["--label", "g", *argv, str(frame)])

    assert info.value.code == 2
