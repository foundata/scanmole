"""An independent PNM oracle for pixel-exact testing.

A deliberately naive per-pixel reimplementation of the *documented* PNM
contracts (see ``scanmole/pnm.py`` docstrings and ARCHITECTURE.md): raw
P4/P5/P6 parsing, mean brightness, 1-bit conversion and cropping. Tests
compare the production implementation against this oracle; the oracle must
therefore never import or call production helpers. Simplicity beats speed
here, so everything works on per-pixel lists and small images.

Contract points the oracle encodes independently:

- P4 rows are byte-padded; padding bits are don't-care and never counted.
- P5/P6 16-bit samples are big-endian; only the high byte is significant.
- P6 reduces through the green channel for conversion; the whole-image
  mean averages all channels.
- The 1-bit cut for threshold ``t`` is ``min(maxval, max(1, round(t *
  maxval)))``; a pixel is black iff its value is below the cut.
- Crops clamp to the frame; P4 aligns the left edge down to a byte
  boundary while the right edge stays exact; a degenerate or full-frame
  box is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass

_WS = b" \t\r\n"


@dataclass(frozen=True)
class OracleImage:
    """A fully decoded raw PNM image.

    ``samples`` holds one list per row: bits (1 = black) for P4, gray
    values for P5 and ``(r, g, b)`` tuples for P6, always at full sample
    precision (16-bit values are not reduced here).
    """

    kind: str
    width: int
    height: int
    maxval: int
    samples: list[list[int]] | list[list[tuple[int, int, int]]]


def _header(data: bytes, count: int) -> tuple[list[int], int]:
    """Read ``count`` integer header tokens; return them and the raster offset."""
    pos = 2
    values: list[int] = []
    while len(values) < count:
        if pos >= len(data):
            raise ValueError("oracle: header ended early")
        if data[pos] in _WS:
            pos += 1
        elif data[pos] == ord("#"):
            while pos < len(data) and data[pos] != ord("\n"):
                pos += 1
        else:
            start = pos
            while pos < len(data) and data[pos] not in _WS:
                pos += 1
            values.append(int(data[start:pos]))
    return values, pos + 1


def parse(data: bytes) -> OracleImage:
    """Decode a raw PNM buffer into per-pixel values (strict, naive)."""
    kind = data[:2].decode("ascii")
    if kind not in ("P4", "P5", "P6"):
        raise ValueError(f"oracle: not a raw PNM: {kind!r}")
    if kind == "P4":
        (width, height), offset = _header(data, 2)
        maxval = 1
    else:
        (width, height, maxval), offset = _header(data, 3)
    if width <= 0 or height <= 0 or not 0 < maxval < 65536:
        raise ValueError("oracle: bad header values")

    if kind == "P4":
        row_bytes = (width + 7) // 8
        bits: list[list[int]] = []
        for y in range(height):
            row = data[offset + y * row_bytes : offset + (y + 1) * row_bytes]
            if len(row) < row_bytes:
                raise ValueError("oracle: truncated raster")
            bits.append([(row[x // 8] >> (7 - x % 8)) & 1 for x in range(width)])
        return OracleImage(kind, width, height, maxval, bits)

    deep = maxval > 255
    step = 2 if deep else 1
    channels = 3 if kind == "P6" else 1
    row_bytes = width * channels * step

    def sample(base: int) -> int:
        return (data[base] << 8 | data[base + 1]) if deep else data[base]

    if len(data) - offset < row_bytes * height:
        raise ValueError("oracle: truncated raster")
    if kind == "P5":
        gray: list[list[int]] = [
            [sample(offset + y * row_bytes + x * step) for x in range(width)]
            for y in range(height)
        ]
        return OracleImage(kind, width, height, maxval, gray)
    rgb: list[list[tuple[int, int, int]]] = [
        [
            (
                sample(offset + y * row_bytes + (x * 3) * step),
                sample(offset + y * row_bytes + (x * 3 + 1) * step),
                sample(offset + y * row_bytes + (x * 3 + 2) * step),
            )
            for x in range(width)
        ]
        for y in range(height)
    ]
    return OracleImage(kind, width, height, maxval, rgb)


def _significant(value: int, maxval: int) -> tuple[int, int]:
    """Reduce a sample to its documented significant part: high byte if deep."""
    if maxval > 255:
        return value >> 8, maxval >> 8
    return value, maxval


def mean(image: OracleImage) -> float:
    """Mean brightness in [0, 1]; P6 averages all channels."""
    if image.kind == "P4":
        black = sum(bit for row in image.samples for bit in row if isinstance(bit, int))
        return 1.0 - black / (image.width * image.height)
    top = _significant(0, image.maxval)[1]
    total = 0
    count = 0
    for row in image.samples:
        for value in row:
            for channel in value if isinstance(value, tuple) else (value,):
                total += _significant(channel, image.maxval)[0]
                count += 1
    return total / (count * top) if count else 0.0


def _conversion_value(
    value: int | tuple[int, int, int], maxval: int
) -> tuple[int, int]:
    """The gray value 1-bit conversion judges: green channel, high byte."""
    gray = value[1] if isinstance(value, tuple) else value
    return _significant(gray, maxval)


def pack_bits(bits: list[list[int]], width: int) -> bytes:
    """Pack per-pixel bit rows into a P4 raster with zeroed padding."""
    row_bytes = (width + 7) // 8
    out = bytearray()
    for row in bits:
        packed = bytearray(row_bytes)
        for x, bit in enumerate(row):
            if bit:
                packed[x // 8] |= 0x80 >> (x % 8)
        out += packed
    return bytes(out)


def binarize(image: OracleImage, threshold: float) -> bytes:
    """The expected complete P4 file for a converted P5/P6 image."""
    if image.kind == "P4":
        raise ValueError("oracle: cannot binarize 1-bit input")
    _, top = _significant(0, image.maxval)
    cut = min(top, max(1, round(threshold * top)))
    bits = [
        [1 if _conversion_value(value, image.maxval)[0] < cut else 0 for value in row]
        for row in image.samples
    ]
    header = b"P4\n%d %d\n" % (image.width, image.height)
    return header + pack_bits(bits, image.width)


def crop(image: OracleImage, box: tuple[int, int, int, int]) -> bytes | None:
    """The expected complete file after cropping, or ``None`` for a no-op."""
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)
    if image.kind == "P4":
        x0 = (x0 // 8) * 8
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    if (x0, y0, x1, y1) == (0, 0, image.width, image.height):
        return None

    rows = [row[x0:x1] for row in image.samples[y0:y1]]
    width, height = x1 - x0, y1 - y0
    if image.kind == "P4":
        header = b"P4\n%d %d\n" % (width, height)
        return header + pack_bits(rows, width)  # type: ignore[arg-type]

    deep = image.maxval > 255
    payload = bytearray()
    for row in rows:
        for value in row:
            for channel in value if isinstance(value, tuple) else (value,):
                if deep:
                    payload += bytes((channel >> 8, channel & 0xFF))
                else:
                    payload.append(channel)
    magic = b"P6" if image.kind == "P6" else b"P5"
    header = b"%s\n%d %d\n%d\n" % (magic, width, height, image.maxval)
    return header + bytes(payload)
