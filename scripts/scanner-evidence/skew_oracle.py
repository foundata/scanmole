#!/usr/bin/env python3
"""Measure residual page skew from printed rules, without any OCR.

The qualification gate for a backend deskew mechanism cannot use
Tesseract as its oracle: Tesseract is what ScanMole's own path measures
with, so grading one against the other would only prove they agree.
This measures geometry instead.

The R1 recurrence sheet of the repository's print pack carries three
rules 60 mm apart, spanning most of the page width and parallel to the
top edge. That spacing is what identifies them: a triplet whose gaps
match the printed sheet is the printed sheet, whereas a full-width dark
band at a frame edge, a border or a stray rule is not, however straight
it looks. Identity from the target beats a rule about one scanner's
artifact, which is why the earlier edge-margin exclusion is gone.

Two numbers come out of every page and they mean different things:

- the **rigid residual**, the median of the rules' angles, which is the
  page rotation a deskew mechanism is responsible for removing;
- the **non-rigid spread**, the widest disagreement between those rules,
  which is the sheet arriving deformed rather than merely turned.

A single rotation cannot remove deformation, so the spread is reported
beside the residual and compared between mechanisms. It is never
subtracted from the residual and never excuses it.

Reads raw PNM (P4, P5, P6) frames, which is what the capture wrapper
writes. Frames stay where they are and are only ever read. Output
carries measurements and operator-supplied labels, never file paths,
device identifiers or timestamps, so a report can be pasted into an
issue or a runbook without redaction. Fully deterministic: the same
frames always produce the same numbers.

This covers the **angle** part of qualification only. A clean exit here
says the geometry passed, not that a mechanism is qualified: retained
targets, blank verdicts, borders, raster depth, sharpness and failure
counts are judged separately, per the runbook.

usage:
  skew_oracle.py --label gray-300-cw --expect 5 FRAME [FRAME ...]
  skew_oracle.py --label bw-300-cw --expect 5 --tsv out.tsv FRAME [...]
"""

from __future__ import annotations

import argparse
import itertools
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

SLABS = 24
"""Vertical slabs the frame is split into before anything is measured.

A rule crossing the whole page is not horizontal in a skewed frame and
never fills a single raster row, which is what makes a naive row scan
miss exactly the frames worth measuring. Within one slab it is nearly
horizontal (over a twenty-fourth of the width even three degrees rises
under four pixels), so each slab can find it as a row, and the fit runs
over the slab centres instead of over columns.
"""

MIN_SLAB_COVERAGE = 0.85
"""How much of a slab's width a rule must fill inside that slab.

The printed rules are solid, so within a slab they cover it almost
entirely; text breaks between glyphs and cannot. Measured over the
corpus this separates them without a tuned value in between.
"""

MIN_TRACK_SLABS = 12
"""How many slabs a rule must appear in to be fitted.

Half the width. Fewer means something interrupted it, and a rule
measured over part of the page is a worse estimate than none.
"""

MAX_TRACK_STEP_MM = 1.5
"""How far a rule may move between neighbouring slabs.

Bounds the linking, and at the same time bounds the angle this can
measure at all: 1.5 mm over a slab of about 7.5 mm is roughly eleven
degrees. In practice MIN_SLAB_COVERAGE fails first and well below that,
because a rule tilted far enough within a slab no longer fills one row
to the required width. Measured hand-fed on one iX500 session
(2026-08-25): Gray frames measured cleanly at 0.84 to 0.98 degrees and
reported no angle at 2.50 degrees and above; Lineart frames reported no
angle from 3.12 degrees up (not bracketed below that). Gray's
anti-aliased rules narrow under the coverage threshold sooner than
Lineart's solid ones. Feed gently for an arm this oracle needs to grade
unmodified; past its range it reports no angle rather than a wrong one.
"""

RULE_SPACING_MM = 60.0
"""Distance between neighbouring rules on the R1 sheet.

A property of the repository's own print pack, so it is a fact about the
target rather than a tuned value. Measured over every R1 frame in the
corpora, neighbouring rules came back 0.009 to 0.526 mm from this.
"""

RULE_SPACING_TOLERANCE_MM = 3.0
"""How far a gap may sit from its nominal multiple and still count.

Roughly six times the worst error measured over the corpora, and scaled
by the multiple, because the gap between the outer two rules carries
both of their position errors (measured up to 3.8 mm across a skewed
sheet). Wide enough for any real feed, far too tight for an artifact to
land on both gaps of a triplet by chance.
"""

RULES_PER_SHEET = 3
"""How many rules a complete R1 measurement rests on.

Two still yield an angle, and the corpus shows a genuine R1 frame losing
one at high skew, so a two-rule result is reported rather than discarded.
It is not complete evidence, and qualification demands complete evidence.
"""

MIN_RULES = 2
"""Below this a frame reports no angle rather than a weak one.

One rule is a measurement with no way to notice it went wrong; two
disagree visibly when a page tears, folds or is cropped mid-rule.
"""


@dataclass(frozen=True)
class Frame:
    """One decoded raster, as bit rows of "is this pixel dark"."""

    width: int
    height: int
    rows: list[bytes]


@dataclass(frozen=True)
class Measurement:
    """What one frame gave up.

    ``rigid`` is the page rotation a mechanism owns; ``spread`` is how
    much the sheet deformed on its way through, which no single rotation
    can remove. Both are ``None`` when the printed rules could not be
    identified at all, which is missing evidence rather than a pass.
    """

    label: str
    rigid: float | None
    spread: float | None
    rules: int
    angles: tuple[float, ...] = ()
    note: str = ""

    @property
    def complete(self) -> bool:
        """Whether all three printed rules were identified."""
        return self.rules == RULES_PER_SHEET


def _tokens(data: bytes, count: int) -> tuple[list[int], int]:
    """Read ``count`` header integers; return them and the raster offset."""
    values: list[int] = []
    position = 2
    while len(values) < count:
        if position >= len(data):
            raise ValueError("header ended early")
        byte = data[position]
        if byte in b" \t\r\n":
            position += 1
        elif byte == ord("#"):
            while position < len(data) and data[position] != ord("\n"):
                position += 1
        else:
            start = position
            while position < len(data) and data[position] not in b" \t\r\n":
                position += 1
            values.append(int(data[start:position]))
    return values, position + 1


def read_frame(path: Path, cutoff: float = 0.5) -> Frame:
    """Decode a raw PNM into per-pixel darkness.

    Raises:
        ValueError: If the file is not a raw PNM this can read.
    """
    data = path.read_bytes()
    magic = data[:2]
    if magic == b"P4":
        (width, height), offset = _tokens(data, 2)
        row_bytes = (width + 7) // 8
        rows = []
        for y in range(height):
            raw = data[offset + y * row_bytes : offset + (y + 1) * row_bytes]
            rows.append(
                bytes(1 if raw[x // 8] >> (7 - x % 8) & 1 else 0 for x in range(width))
            )
        return Frame(width, height, rows)
    if magic not in (b"P5", b"P6"):
        raise ValueError(f"not a raw PNM: {magic!r}")
    (width, height, maxval), offset = _tokens(data, 3)
    channels = 3 if magic == b"P6" else 1
    step = 2 if maxval > 255 else 1
    stride = width * channels * step
    top = maxval >> 8 if maxval > 255 else maxval
    cut_value = int(cutoff * top)
    rows = []
    for y in range(height):
        base = offset + y * stride
        # Green for color, the high byte for 16-bit: the same reduction
        # the engine documents for its own 1-bit conversion.
        pick = base + (step if channels == 3 else 0)
        rows.append(
            bytes(
                1 if data[pick + x * channels * step] < cut_value else 0
                for x in range(width)
            )
        )
    return Frame(width, height, rows)


def _slab_rows(frame: Frame, first: int, last: int) -> list[float]:
    """Sub-pixel row positions of every rule crossing one slab.

    A rule may straddle two raster rows inside the slab, so adjacent
    qualifying rows are merged and weighted by how much of the slab each
    one holds. That weighting is where the sub-pixel precision comes
    from on a 1-bit frame, which has no gray to interpolate with.
    """
    width = last - first + 1
    needed = int(width * MIN_SLAB_COVERAGE)
    counts = [
        (y, total)
        for y, row in enumerate(frame.rows)
        if (total := sum(row[first : last + 1])) >= needed
    ]
    positions: list[float] = []
    index = 0
    while index < len(counts):
        end_index = index
        while (
            end_index + 1 < len(counts)
            and counts[end_index + 1][0] == counts[end_index][0] + 1
        ):
            end_index += 1
        group = counts[index : end_index + 1]
        mass = sum(total for _y, total in group)
        positions.append(sum(y * total for y, total in group) / mass)
        index = end_index + 1
    return positions


def _tracks(frame: Frame, dpi: int) -> list[list[tuple[float, float]]]:
    """Link per-slab rule positions into one track per printed rule."""
    step = max(1, frame.width // SLABS)
    reach = MAX_TRACK_STEP_MM * dpi / 25.4
    open_tracks: list[list[tuple[float, float]]] = []
    done: list[list[tuple[float, float]]] = []
    for slab in range(SLABS):
        first = slab * step
        last = min(frame.width - 1, first + step - 1)
        if last <= first:
            break
        centre = (first + last) / 2
        found = _slab_rows(frame, first, last)
        still_open: list[list[tuple[float, float]]] = []
        for track in open_tracks:
            nearest = min(found, key=lambda y: abs(y - track[-1][1]), default=None)
            if nearest is not None and abs(nearest - track[-1][1]) <= reach:
                track.append((centre, nearest))
                found.remove(nearest)
                still_open.append(track)
            else:
                done.append(track)
        open_tracks = still_open + [[(centre, y)] for y in found]
    return [track for track in done + open_tracks if len(track) >= MIN_TRACK_SLABS]


def _fit(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of a track, as an angle in degrees."""
    mean_x = sum(x for x, _y in points) / len(points)
    mean_y = sum(y for _x, y in points) / len(points)
    variance = sum((x - mean_x) ** 2 for x, _y in points)
    if variance == 0:
        return None
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return math.degrees(math.atan(covariance / variance))


def _matches(gap: float, multiple: int, dpi: float) -> bool:
    """Whether one gap sits at ``multiple`` times the printed spacing."""
    nominal = RULE_SPACING_MM * multiple * dpi / 25.4
    return abs(gap - nominal) <= RULE_SPACING_TOLERANCE_MM * multiple * dpi / 25.4


def _printed_rules(
    tracks: list[list[tuple[float, float]]], dpi: float
) -> tuple[list[list[tuple[float, float]]], str]:
    """The tracks that are the R1 sheet's own rules, by their spacing.

    Prefers a full triplet whose two gaps both match the printed 60 mm,
    and falls back to a pair one printed spacing apart, which the corpus
    shows a genuine frame producing at high skew. Everything else is an
    artifact whatever it looks like: a frame-edge shadow spans the width
    and is perfectly straight, and is exactly what this must not accept
    in place of a missing target.

    Returns:
        The selected tracks top to bottom, and a note when the listing
        was ambiguous rather than merely incomplete.
    """
    ordered = sorted(tracks, key=lambda t: sum(y for _x, y in t) / len(t))
    centres = [sum(y for _x, y in t) / len(t) for t in ordered]

    triplets = [
        combo
        for combo in itertools.combinations(range(len(ordered)), RULES_PER_SHEET)
        if _matches(centres[combo[1]] - centres[combo[0]], 1, dpi)
        and _matches(centres[combo[2]] - centres[combo[1]], 1, dpi)
    ]
    if len(triplets) > 1:
        return [], "more than one triplet matches the printed spacing"
    if triplets:
        return [ordered[index] for index in triplets[0]], ""

    pairs = [
        combo
        for combo in itertools.combinations(range(len(ordered)), 2)
        if any(
            _matches(centres[combo[1]] - centres[combo[0]], multiple, dpi)
            for multiple in (1, 2)
        )
    ]
    if len(pairs) > 1:
        return [], "more than one pair matches the printed spacing"
    if pairs:
        return [ordered[index] for index in pairs[0]], ""
    return [], "no rules at the printed spacing; is this an R1 sheet?"


def measure(path: Path, dpi: int, label: str) -> Measurement:
    """The rigid residual and non-rigid spread of one frame."""
    try:
        frame = read_frame(path)
    except (OSError, ValueError, IndexError) as exc:
        return Measurement(label, None, None, 0, note=f"unreadable ({exc})")
    selected, note = _printed_rules(_tracks(frame, dpi), dpi)
    angles = [angle for track in selected if (angle := _fit(track)) is not None]
    if len(angles) < MIN_RULES:
        return Measurement(label, None, None, len(angles), note=note or "too few rules")
    return Measurement(
        label,
        statistics.median(angles),
        max(angles) - min(angles),
        len(angles),
        tuple(angles),
    )


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; deterministic and dependency-free."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def report(measurements: list[Measurement], label: str, expected: int) -> int:
    """Print the verdict for one group; return the process exit code.

    Evidence has to be complete before it can be good. A frame nobody
    could measure is missing evidence, not a frame that quietly leaves
    the denominator, so it fails the group rather than improving its
    percentages.
    """
    usable = [m for m in measurements if m.rigid is not None]
    print(f"group: {label}")
    print(f"frames supplied: {len(measurements)}, expected: {expected}")
    print(f"frames measured: {len(usable)}")
    for entry in measurements:
        if entry.rigid is None:
            print(f"  {entry.label}: no angle ({entry.note})")
        elif not entry.complete:
            print(
                f"  {entry.label}: only {entry.rules} of {RULES_PER_SHEET} "
                "printed rules identified"
            )

    complete = [
        m
        for m in measurements
        if m.rigid is not None and m.spread is not None and m.complete
    ]
    checks: list[tuple[str, bool]] = [
        ("every expected frame supplied", len(measurements) >= expected),
        ("every supplied frame measurable", len(usable) == len(measurements)),
        (
            f"every frame shows all {RULES_PER_SHEET} printed rules",
            len(complete) == len(measurements),
        ),
    ]
    if complete:
        rigid = [abs(m.rigid) for m in complete if m.rigid is not None]
        signed = [m.rigid for m in complete if m.rigid is not None]
        spread = [m.spread for m in complete if m.spread is not None]
        within = sum(1 for value in rigid if value <= 0.10) / len(rigid)
        print("\nrigid residual (the rotation a mechanism owns):")
        print(f"  median |residual| : {statistics.median(rigid):.3f} deg")
        print(f"  signed median     : {statistics.median(signed):+.3f} deg")
        print(f"  95th percentile   : {_percentile(rigid, 0.95):.3f} deg")
        print(f"  worst             : {max(rigid):.3f} deg")
        print(f"  at or below 0.10  : {within:.1%}")
        # Reported beside the residual and never subtracted from it: no
        # rotation can remove a sheet that arrived deformed, and no
        # amount of deformation excuses leaving the rotation in.
        print("\nnon-rigid spread (deformation no rotation can remove):")
        print(f"  median            : {statistics.median(spread):.3f} deg")
        print(f"  95th percentile   : {_percentile(spread, 0.95):.3f} deg")
        print(f"  worst             : {max(spread):.3f} deg")
        print("  compare this between the backend-off, host and backend arms;")
        print("  a mechanism must not make it materially worse than host.")
        checks += [
            ("at least 95% of rigid residuals within 0.10 deg", within >= 0.95),
            ("no rigid residual above 0.20 deg", max(rigid) <= 0.20),
            (
                "|signed median| of rigid residuals at or below 0.05 deg",
                abs(statistics.median(signed)) <= 0.05,
            ),
        ]
    print()
    for name, passed in checks:
        print(f"  [{'pass' if passed else 'FAIL'}] {name}")
    passed_all = bool(complete) and all(passed for _name, passed in checks)
    print(
        "\nangle criteria passed; qualification also needs retained targets, "
        "blank verdicts, borders, raster depth, sharpness and failure counts "
        "(see the runbook)."
        if passed_all
        else "\nangle criteria NOT met; this evidence does not support qualification."
    )
    return 0 if passed_all else 1


def main(argv: list[str] | None = None) -> int:
    """Measure the frames named on the command line."""
    parser = argparse.ArgumentParser(
        prog="skew_oracle.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("frames", nargs="+", type=Path, metavar="FRAME")
    parser.add_argument(
        "--label",
        required=True,
        help="name of this group (mode, dpi and direction), used in the report",
    )
    parser.add_argument(
        "--expect",
        type=int,
        required=True,
        metavar="N",
        help="how many frames this group must contain; fewer fails the group",
    )
    parser.add_argument(
        "-r", "--resolution", type=int, default=300, metavar="DPI", help="frame dpi"
    )
    parser.add_argument(
        "--tsv",
        type=Path,
        metavar="FILE",
        help="also write one row per frame, identified by ordinal only",
    )
    args = parser.parse_args(argv)
    if args.resolution <= 0:
        parser.error("--resolution must be positive")
    if args.expect <= 0:
        parser.error("--expect must be positive")
    measurements = [
        measure(path, args.resolution, f"{args.label}#{index:02d}")
        for index, path in enumerate(sorted(args.frames), 1)
    ]
    if args.tsv is not None:
        lines = ["label\trigid\tspread\trules\tangles\tnote"]
        lines += [
            f"{m.label}\t"
            f"{'' if m.rigid is None else f'{m.rigid:+.4f}'}\t"
            f"{'' if m.spread is None else f'{m.spread:.4f}'}\t"
            f"{m.rules}\t"
            f"{' '.join(f'{a:+.4f}' for a in m.angles)}\t{m.note}"
            for m in measurements
        ]
        args.tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report(measurements, args.label, args.expect)


if __name__ == "__main__":
    sys.exit(main())
