#!/usr/bin/env python3
"""Inventory raw PNM scanner frames: geometry, sizes and checksums.

Standalone stdlib tool for the scanner evidence kit (usable without
installing ScanMole). Reads P4/P5/P6 files and writes one deterministic
tab-separated row per valid file to stdout; parse failures go to stderr
and turn the exit status nonzero, without stopping the remaining files.

Trailing raster bytes are reported, not rejected: some backends (the
ScanSnap iX500 among them) occasionally deliver one raster row beyond
the declared header height, and the inventory's job is to expose that
condition exactly as captured.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

COLUMNS = (
    "file",
    "format",
    "width",
    "height",
    "maxval",
    "bytes",
    "expected_raster",
    "trailing",
    "sha256",
)


class PnmError(ValueError):
    """A file is not a valid P4/P5/P6 image."""


@dataclass(frozen=True)
class PnmHeader:
    """The parsed header of a binary PNM file.

    ``maxval`` is ``None`` for P4 (1-bit files carry no maxval token).
    ``raster_offset`` is the index of the first raster byte.
    """

    magic: str
    width: int
    height: int
    maxval: int | None
    raster_offset: int

    @property
    def raster_bytes(self) -> int:
        """The raster size the header promises, in bytes."""
        if self.magic == "P4":
            return ((self.width + 7) // 8) * self.height
        assert self.maxval is not None
        samples = 3 if self.magic == "P6" else 1
        return self.width * self.height * samples * (2 if self.maxval > 255 else 1)


def parse_header(data: bytes) -> PnmHeader:
    """Parse a binary PNM header, tolerating comments and any whitespace.

    Raises:
        PnmError: If the magic, dimensions or maxval are invalid, or the
            header is truncated.
    """
    magic = data[:2]
    if magic not in (b"P4", b"P5", b"P6"):
        raise PnmError(f"not a binary PNM (magic {magic!r})")
    needed = 2 if magic == b"P4" else 3
    tokens: list[bytes] = []
    position = 2
    while len(tokens) < needed:
        while position < len(data) and data[position : position + 1].isspace():
            position += 1
        if data[position : position + 1] == b"#":
            while position < len(data) and data[position : position + 1] != b"\n":
                position += 1
            continue
        start = position
        while position < len(data) and not data[position : position + 1].isspace():
            position += 1
        if position == start:
            raise PnmError("truncated PNM header")
        tokens.append(data[start:position])
    if position >= len(data):
        raise PnmError("truncated PNM header")
    position += 1  # exactly one whitespace byte separates header and raster

    try:
        width, height = int(tokens[0]), int(tokens[1])
    except ValueError as exc:
        raise PnmError("bad PNM dimensions") from exc
    if width <= 0 or height <= 0:
        raise PnmError("bad PNM dimensions")
    maxval: int | None = None
    if magic != b"P4":
        try:
            maxval = int(tokens[2])
        except ValueError as exc:
            raise PnmError("bad PNM maxval") from exc
        if not 0 < maxval < 65536:
            raise PnmError("bad PNM maxval")
    return PnmHeader(magic.decode(), width, height, maxval, position)


def inventory_row(path: Path) -> tuple[str, ...]:
    """One inventory row for ``path``, in :data:`COLUMNS` order.

    Raises:
        PnmError: If the file is malformed or its raster is truncated.
        OSError: If the file cannot be read.
    """
    data = path.read_bytes()
    header = parse_header(data)
    available = len(data) - header.raster_offset
    if available < header.raster_bytes:
        raise PnmError(f"truncated raster: {available} of {header.raster_bytes} bytes")
    return (
        path.name,
        header.magic,
        str(header.width),
        str(header.height),
        "-" if header.maxval is None else str(header.maxval),
        str(len(data)),
        str(header.raster_bytes),
        str(available - header.raster_bytes),
        hashlib.sha256(data).hexdigest(),
    )


def main(argv: list[str] | None = None) -> int:
    """Write the inventory for the given files; nonzero if any is invalid."""
    files = sys.argv[1:] if argv is None else argv
    if not files:
        print("usage: pnm_inventory.py FILE...", file=sys.stderr)
        return 2
    failures = 0
    print("\t".join(COLUMNS))
    for name in files:
        try:
            print("\t".join(inventory_row(Path(name))))
        except (PnmError, OSError) as exc:
            failures += 1
            print(f"error: {name}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
