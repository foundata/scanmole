#!/usr/bin/env python3
"""Deterministic test-sheet pack for scanner evidence capture.

Generates ``print-pack.ps`` next to this script: one self-contained
PostScript document (built-in fonts, no external resources, no dates, no
third-party text; the filler is an original neutral word cycle).
Regeneration is byte-identical, so the committed file is verifiable with
``--check``.

Page plan (also embedded as comments in the PostScript):

  pages 1-12   six dense sheets D1-D6, print TWO-SIDED LONG EDGE
  pages 13-24  twelve single-sided sheets, print ONE-SIDED:
               S1-S4 (dense fronts whose factory-blank backs are the
               evidence), F1 footer, P1 sparse, the page-number sheet,
               U1/U2 punch sources, R1 recurrence, A1 A5 cut guide,
               T1 receipt strip

Print at 100% scale, never fit-to-page. Intentionally blank faces (the
backs of pages 13-24) stay completely unprinted; a fully blank sheet is
never printed at all and must come straight from a clean paper pack.
"""

from __future__ import annotations

import sys
from pathlib import Path

POINTS_PER_MM = 72 / 25.4
A4_WIDTH_MM, A4_HEIGHT_MM = 210.0, 297.0
PAGE_COUNT = 24

WORDS = (
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _mm(value: float) -> str:
    return f"{value * POINTS_PER_MM:.2f}"


class _Page:
    """Collects PostScript operators for one page, coordinates in mm."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        size: float,
        string: str,
        font: str = "Courier",
        center_at: float | None = None,
    ) -> None:
        self.ops.append(f"/{font} findfont {size} scalefont setfont")
        if center_at is not None:
            self.ops.append(
                f"({_escape(string)}) dup stringwidth pop 2 div "
                f"{_mm(center_at)} exch sub {_mm(y)} moveto show"
            )
        else:
            self.ops.append(f"{_mm(x)} {_mm(y)} moveto ({_escape(string)}) show")

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        width: float = 0.2,
        dashed: bool = False,
    ) -> None:
        dash = "[3 3] 0 setdash" if dashed else "[] 0 setdash"
        self.ops.append(
            f"gsave {width * POINTS_PER_MM:.2f} setlinewidth {dash} "
            f"{_mm(x0)} {_mm(y0)} moveto {_mm(x1)} {_mm(y1)} lineto stroke grestore"
        )

    def triangle_up(self, x: float, y: float, size: float = 3.0) -> None:
        half = size / 2
        self.ops.append(
            f"gsave newpath {_mm(x - half)} {_mm(y)} moveto "
            f"{_mm(x + half)} {_mm(y)} lineto {_mm(x)} {_mm(y + size)} lineto "
            "closepath fill grestore"
        )

    def square(
        self, x: float, y: float, size: float = 4.0, filled: bool = True
    ) -> None:
        op = "fill" if filled else "stroke"
        self.ops.append(
            f"gsave newpath {_mm(x)} {_mm(y)} moveto {_mm(x + size)} {_mm(y)} lineto "
            f"{_mm(x + size)} {_mm(y + size)} lineto {_mm(x)} {_mm(y + size)} lineto "
            f"closepath 0.3 setlinewidth {op} grestore"
        )

    def circle(self, x: float, y: float, radius: float = 2.0) -> None:
        self.ops.append(
            f"gsave newpath {_mm(x)} {_mm(y)} {_mm(radius)} 0 360 arc fill grestore"
        )

    def cross(self, x: float, y: float, size: float = 4.0) -> None:
        half = size / 2
        self.line(x - half, y, x + half, y, width=0.6)
        self.line(x, y - half, x, y + half, width=0.6)


def _filler(page_id: str, index: int, width: int = 88) -> str:
    words = [WORDS[(index + offset) % len(WORDS)] for offset in range(12)]
    line = f"{page_id} {index:02d} " + " ".join(words)
    while len(line) < width:
        line += " " + WORDS[index % len(WORDS)]
    return line[:width]


def _dense_body(
    page: _Page,
    page_id: str,
    left: float = 20.0,
    top: float = 268.0,
    bottom: float = 25.0,
) -> None:
    y = top
    index = 0
    while y >= bottom:
        page.text(left, y, 9, _filler(page_id, index))
        y -= 5.0
        index += 1


def _header(page: _Page, big: str, small_id: str, note: str = "") -> None:
    page.triangle_up(20, 288)
    page.text(25, 288.5, 8, "TOP", font="Helvetica")
    page.text(0, 279, 16, big, font="Helvetica-Bold", center_at=A4_WIDTH_MM / 2)
    if note:
        page.text(0, 273, 9, note, font="Helvetica", center_at=A4_WIDTH_MM / 2)
    page.text(12, 289, 7, small_id)  # top-left corner id
    page.text(186, 8, 7, small_id)  # bottom-right corner id: flags 180 degrees


def _dense_sheet(big: str, small_id: str, note: str) -> _Page:
    page = _Page()
    _header(page, big, small_id, note)
    _dense_body(page, small_id)
    return page


def _footer_sheet() -> _Page:
    page = _Page()
    _header(
        page, "F1 FOOTER SHEET", "F1", "the last line sits 8 mm from the bottom edge"
    )
    _dense_body(page, "F1", bottom=30.0)
    page.text(20, 8, 9, _filler("F1-FOOTER-8MM", 99))
    return page


def _sparse_sheet() -> _Page:
    # Deliberately minimal: the single line is both the evidence and the
    # only identifier, so nothing else may dilute the sparse verdict.
    page = _Page()
    page.text(
        30,
        210,
        11,
        "P1 sparse sheet: one short line in the upper third",
        font="Helvetica",
    )
    return page


def _page_number_sheet() -> _Page:
    # Only a bottom-centered "17"; the number itself is the identifier.
    page = _Page()
    page.text(0, 12, 11, "17", font="Helvetica", center_at=A4_WIDTH_MM / 2)
    return page


def _punch_sheet(number: int) -> _Page:
    page_id = f"U{number}"
    page = _Page()
    _header(
        page,
        f"{page_id} PUNCH SHEET",
        page_id,
        "punch 2 holes into the LEFT edge after printing",
    )
    _dense_body(page, page_id, left=30.0)
    return page


def _recurrence_sheet() -> _Page:
    # Four distinct corner targets: unambiguous under 180-degree feeding.
    page = _Page()
    for x in (70, 105, 140):
        page.triangle_up(x, 288)
    page.text(0, 283, 9, "TOP EDGE", font="Helvetica", center_at=A4_WIDTH_MM / 2)
    page.square(13, 278, filled=True)  # top-left: filled square
    page.square(193, 278, filled=False)  # top-right: open square
    page.circle(15, 15)  # bottom-left: filled circle
    page.cross(195, 15)  # bottom-right: cross
    page.text(25, 271, 7, "R1")
    page.text(180, 22, 7, "R1")
    for offset in (60, 120, 180):  # thin rules, measured from the top edge
        page.line(15, A4_HEIGHT_MM - offset, 195, A4_HEIGHT_MM - offset)
    y = 200.0
    index = 0
    while y >= 60.0:
        page.text(30, y, 9, _filler("R1", index, width=72))
        y -= 5.0
        index += 1
    return page


def _a5_sheet() -> _Page:
    # Rotated frame via translate(210, 148.5) + rotate 90: x runs up the
    # sheet through the top half, y from the right print edge leftward.
    # Glyph tops face the printed left edge, so the cut half reads
    # upright when fed left-print-edge first (A5 portrait, 148.5 mm).
    page = _Page()
    block = _Page()
    block.triangle_up(20, 138)
    block.text(25, 138.5, 8, "TOP", font="Helvetica")
    block.text(0, 128, 13, "A1 A5 SHEET", font="Helvetica-Bold", center_at=74.25)
    y = 118.0
    index = 0
    while y >= 15.0:
        block.text(12, y, 8, _filler("A1", index, width=52))
        y -= 5.0
        index += 1
    page.ops.append(
        f"gsave {_mm(A4_WIDTH_MM)} {_mm(A4_HEIGHT_MM / 2)} translate 90 rotate"
    )
    page.ops.extend(block.ops)
    page.ops.append("grestore")
    page.line(0, A4_HEIGHT_MM / 2, A4_WIDTH_MM, A4_HEIGHT_MM / 2, dashed=True)
    page.text(
        20,
        130,
        10,
        "CUT along the dashed line. Keep the printed half",
        font="Helvetica",
    )
    page.text(
        20, 124, 10, "and feed it with its TOP arrow entering first", font="Helvetica"
    )
    page.text(20, 118, 10, "(that is the printed left-hand edge).", font="Helvetica")
    page.text(20, 106, 10, "Recycle this unprinted half.", font="Helvetica")
    return page


def _receipt_sheet() -> _Page:
    page = _Page()
    y = 280.0
    index = 0
    while y >= 15.0:
        page.text(6, y, 8, _filler("T1", index, width=32))
        y -= 4.5
        index += 1
    page.line(75, 0, 75, A4_HEIGHT_MM, dashed=True)
    page.text(85, 270, 10, "CUT along the dashed line.", font="Helvetica")
    page.text(85, 264, 10, "Keep the left 75 mm strip (T1),", font="Helvetica")
    page.text(85, 258, 10, "recycle this part.", font="Helvetica")
    return page


def _pages() -> list[tuple[str, _Page]]:
    """All pack pages with a one-line plan comment per page."""
    pages: list[tuple[str, _Page]] = []
    for number in range(1, 7):
        pages.append(
            (
                f"sheet D{number} front (duplex section, two-sided long edge)",
                _dense_sheet(
                    f"D{number} FRONT",
                    f"D{number}F",
                    f"double-sided sheet {number} of 6",
                ),
            )
        )
        pages.append(
            (
                f"sheet D{number} back",
                _dense_sheet(
                    f"D{number} BACK",
                    f"D{number}B",
                    f"back of double-sided sheet {number} of 6",
                ),
            )
        )
    for number in range(1, 5):
        pages.append(
            (
                f"sheet S{number} (simplex section; its blank back is evidence)",
                _dense_sheet(
                    f"S{number} SINGLE-SIDED",
                    f"S{number}",
                    "KEEP THE BACK FACTORY BLANK",
                ),
            )
        )
    pages.append(("footer sheet F1", _footer_sheet()))
    pages.append(("sparse sheet P1", _sparse_sheet()))
    pages.append(("page-number sheet (prints only '17')", _page_number_sheet()))
    pages.append(("punch source U1", _punch_sheet(1)))
    pages.append(("punch source U2", _punch_sheet(2)))
    pages.append(("recurrence sheet R1", _recurrence_sheet()))
    pages.append(("A5 cut guide A1", _a5_sheet()))
    pages.append(("receipt strip source T1 (optional)", _receipt_sheet()))
    return pages


def render_pack() -> str:
    """The complete PostScript document as deterministic text."""
    pages = _pages()
    out = [
        "%!PS-Adobe-3.0",
        "%%Title: scanmole scanner evidence print pack",
        "%%Creator: scripts/scanner-evidence/print_pack.py",
        f"%%Pages: {len(pages)}",
        "%%BoundingBox: 0 0 596 842",
        "%%DocumentMedia: A4 595.28 841.89 0 () ()",
        "%%EndComments",
        "% Print pages 1-12 two-sided (long edge) and pages 13-24 one-sided,",
        "% both at 100% scale without fit-to-page. The backs of pages 13-24",
        "% stay factory blank on purpose; a fully blank sheet is never",
        "% printed and comes straight from a clean paper pack.",
        "<< /PageSize [595.28 841.89] >> setpagedevice",
    ]
    for number, (plan, page) in enumerate(pages, 1):
        out.append(f"%%Page: {number} {number}")
        out.append(f"% {plan}")
        out.append("0 setgray")
        out.extend(page.ops)
        out.append("showpage")
    out.append("%%EOF")
    return "\n".join(out) + "\n"


def pack_path() -> Path:
    """The committed destination next to this script."""
    return Path(__file__).parent / "print-pack.ps"


def main(argv: list[str] | None = None) -> int:
    """Generate ``print-pack.ps``, or verify it with ``--check``."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--check"]):
        print("usage: print_pack.py [--check]", file=sys.stderr)
        return 2
    rendered = render_pack()
    destination = pack_path()
    if arguments == ["--check"]:
        try:
            committed = destination.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {destination}: {exc}", file=sys.stderr)
            return 1
        if committed != rendered:
            print(
                f"error: {destination} does not match its generator; "
                "rerun print_pack.py and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"ok: {destination} matches its generator")
        return 0
    destination.write_text(rendered, encoding="utf-8")
    print(f"wrote {destination} ({rendered.count('showpage')} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
