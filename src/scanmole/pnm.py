"""Blank-page detection via mean image brightness.

Raw PNM images (the format ``scanimage`` writes) are parsed with the standard
library alone. Other formats (possible via ``--from-images``) are not
measured: those inputs are user-curated, so blank detection is skipped for
them instead of pulling in an image library.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_WHITESPACE = b" \t\r\n"


def _read_header(buffer: bytes, token_count: int) -> tuple[list[bytes], int]:
    """Read the first ``token_count`` PNM header tokens after the magic number.

    Handles arbitrary whitespace and ``#`` comment lines.

    Returns:
        The tokens and the offset of the first raster byte (one whitespace byte
        past the final header token).

    Raises:
        ValueError: If the header ends before ``token_count`` tokens are read.
    """
    tokens: list[bytes] = []
    index = 2  # past the "P4"/"P5"/"P6" magic number
    size = len(buffer)
    while len(tokens) < token_count:
        while index < size and buffer[index] in _WHITESPACE:
            index += 1
        if index < size and buffer[index] == 0x23:  # "#" comment to end of line
            while index < size and buffer[index] != 0x0A:
                index += 1
            continue
        start = index
        while index < size and buffer[index] not in _WHITESPACE:
            index += 1
        if index == start:
            raise ValueError("truncated PNM header")
        tokens.append(buffer[start:index])
    return tokens, index + 1


def pnm_mean(path: Path) -> float | None:
    """Return the mean brightness (0..1) of a raw PNM image.

    Args:
        path: Image file to inspect.

    Returns:
        The mean brightness for a raw ``P4``/``P5``/``P6`` image, or ``None``
        when the file is not a raw PNM (so a caller can try another method).

    Raises:
        ValueError: If the file starts as a PNM but the header is malformed,
            the dimensions or maxval are invalid, or the raster is truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"4", b"5", b"6"):
        return None
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 2 if kind == b"4" else 3)
    try:
        width, height = int(tokens[0]), int(tokens[1])
    except ValueError as exc:
        raise ValueError("bad PNM dimensions") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")

    if kind == b"4":  # 1 bit per pixel, rows byte-padded, a set bit is black
        row_bytes = (width + 7) // 8
        data = buffer[offset : offset + row_bytes * height]
        if len(data) < row_bytes * height:
            raise ValueError("truncated PNM raster")
        black = int.from_bytes(data, "big").bit_count()
        if width % 8:
            # The spec declares row-padding bits don't-care and some producers
            # leave garbage there; subtract any set bits in the pad positions.
            pad_mask = 0xFF >> (width % 8)
            black -= sum(
                (byte & pad_mask).bit_count()
                for byte in data[row_bytes - 1 :: row_bytes]
            )
        return max(0.0, 1.0 - black / (width * height))

    try:
        maxval = int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM maxval") from exc
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")
    channels = 3 if kind == b"6" else 1
    deep = maxval > 255
    row_bytes = width * channels * (2 if deep else 1)
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = memoryview(buffer)[offset : offset + row_bytes * height]
    if deep:  # 16-bit big-endian: approximate with the high bytes
        raster, row_bytes, maxval = raster[0::2], width * channels, maxval >> 8

    # Subsample rows of very large images; sum(bytes) runs at C speed but O(n).
    step = max(1, len(raster) // (4 * 1024 * 1024))
    total = 0
    counted = 0
    for row in range(0, height, step):
        chunk = raster[row * row_bytes : (row + 1) * row_bytes]
        total += sum(chunk)
        counted += len(chunk)
    return (total / (counted * maxval)) if counted else 0.0


def binarize_pnm(path: Path, threshold: float) -> bool:
    """Convert a raw gray or color PNM into a 1-bit ``P4`` file, in place.

    A pixel darker than ``threshold`` (a fraction of full brightness) becomes
    black. Color (``P6``) input is reduced through its green channel first,
    an adequate luma proxy for documents. 16-bit samples use their high byte.

    Args:
        path: Image file to convert.
        threshold: Black/white cutoff in (0, 1), relative to full brightness.

    Returns:
        Whether the file was rewritten. ``False`` means the file is already
        1-bit or not a raw PNM at all, so there is nothing to convert.

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"5", b"6"):
        return False
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 3)
    try:
        width, height, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")

    channels = 3 if kind == b"6" else 1
    deep = maxval > 255
    row_bytes = width * channels * (2 if deep else 1)
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = buffer[offset : offset + row_bytes * height]
    if deep:  # 16-bit big-endian: the high bytes carry the significant part
        raster, maxval = raster[0::2], maxval >> 8
    if channels == 3:
        raster = raster[1::3]  # green channel

    cut = min(maxval, max(1, round(threshold * maxval)))
    row_out = (width + 7) // 8
    pad = row_out * 8 - width
    if pad:  # pad rows with white so the padding bits stay zero
        raster = b"".join(
            raster[y * width : (y + 1) * width] + b"\xff" * pad for y in range(height)
        )

    # Pack at C speed: byte i of every 8-byte group contributes bit 7-i of one
    # output byte. Each translate maps "darker than cut" to that bit's weight;
    # the big-integer additions cannot carry because the weights are disjoint.
    packed = 0
    for bit in range(8):
        table = bytes(128 >> bit if value < cut else 0 for value in range(256))
        packed += int.from_bytes(raster[bit::8].translate(table), "big")
    data = packed.to_bytes(row_out * height, "big")

    path.write_bytes(b"P4\n%d %d\n" % (width, height) + data)
    return True


PAPER_BRIGHTNESS_CUTOFF = 0.7
"""Column/row mean above which a scan line counts as paper, not backing.

Measured on real hardware: ADF backing scans at roughly 0.35 to 0.55 mean
brightness (gray backing, end-of-paper padding) while paper stays above 0.9,
so the cutoff sits comfortably between the two clusters. Scanners with white
backing produce no edge below the cutoff and the page is simply kept whole.
"""

_MIN_PAPER_PX = 16
"""Reject a detected paper box smaller than this per axis as noise."""


def autocrop_pnm(path: Path, trim_px: int) -> bool:
    """Crop a raw gray/color PNM to the detected paper edges, in place.

    Scanning the device's full window (page size ``auto``) surrounds the
    paper with the darker ADF backing and end-of-paper padding. This walks
    the column and row mean-brightness profiles inward from each edge until
    they cross :data:`PAPER_BRIGHTNESS_CUTOFF`, then rewrites the file
    cropped to that box, shaved inward by ``trim_px`` on every side so the
    half-gray transition pixels cannot survive as a dark rim (which would
    both look bad after 1-bit conversion and rescue blank pages from the
    blank drop). Printed content never sits at the physical paper edge, so
    the shave is safe.

    Already-1-bit (``P4``) and non-PNM files are left alone: devices that
    scan real lineart size their pages in hardware.

    Returns:
        Whether the file was rewritten. ``False`` also covers "no backing
        visible" (borderless scan or white backing) and "no paper found" (a
        safety fallback keeping the full frame).

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"5", b"6"):
        return False
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 3)
    try:
        width, height, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")

    channels = 3 if kind == b"6" else 1
    deep = maxval > 255
    pixel_bytes = channels * (2 if deep else 1)
    row_bytes = width * pixel_bytes
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = buffer[offset : offset + row_bytes * height]

    gray = raster
    if deep:  # 16-bit big-endian: the high bytes carry the significant part
        gray = gray[0::2]
    if channels == 3:
        gray = gray[1::3]  # green channel
    cutoff = PAPER_BRIGHTNESS_CUTOFF * (maxval >> 8 if deep else maxval)

    # Column profile over a row subsample (C-speed slices); row profile over a
    # column subsample restricted to the detected paper columns, so the side
    # backing cannot drag content rows below the cutoff.
    row_step = max(1, height // 512)
    sampled = b"".join(
        gray[row * width : (row + 1) * width] for row in range(0, height, row_step)
    )
    sampled_rows = len(sampled) // width

    def column_is_paper(column: int) -> bool:
        return sum(sampled[column::width]) / sampled_rows >= cutoff

    left, right = 0, width - 1
    while left < right and not column_is_paper(left):
        left += 1
    while right > left and not column_is_paper(right):
        right -= 1
    if right - left < _MIN_PAPER_PX:
        return False  # no plausible paper found; keep the full frame

    column_step = max(1, (right - left + 1) // 512)

    def row_is_paper(row: int) -> bool:
        segment = gray[row * width + left : row * width + right + 1 : column_step]
        return sum(segment) / len(segment) >= cutoff

    top, bottom = 0, height - 1
    # End-of-paper padding can be as bright as paper: some devices pad color
    # and back-side passes with pure white, invisible to the brightness walk.
    # But that padding is synthetic and bit-perfectly uniform, which real
    # scanned paper never is (sensor noise), so first strip the run of rows
    # identical to a perfectly uniform bottom row.
    last_row = gray[(height - 1) * width :]
    if min(last_row) == max(last_row):
        while bottom > top and gray[bottom * width : (bottom + 1) * width] == last_row:
            bottom -= 1
    while top < bottom and not row_is_paper(top):
        top += 1
    while bottom > top and not row_is_paper(bottom):
        bottom -= 1
    if bottom - top < _MIN_PAPER_PX:
        return False

    if (left, top, right, bottom) == (0, 0, width - 1, height - 1):
        return False  # no backing visible anywhere; nothing to crop
    left += trim_px
    right -= trim_px
    top += trim_px
    bottom -= trim_px
    if right - left < _MIN_PAPER_PX or bottom - top < _MIN_PAPER_PX:
        return False

    start = left * pixel_bytes
    stop = (right + 1) * pixel_bytes
    data = b"".join(
        raster[row * row_bytes + start : row * row_bytes + stop]
        for row in range(top, bottom + 1)
    )
    magic = b"P6" if kind == b"6" else b"P5"
    header = b"%s\n%d %d\n%d\n" % (magic, right - left + 1, bottom - top + 1, maxval)
    path.write_bytes(header + data)
    return True


def autocrop_image(path: Path, trim_px: int) -> bool:
    """Best-effort in-place crop to the paper edges; never fails the page.

    Returns:
        Whether the file was cropped. A malformed file is left untouched with
        a warning, so the page still reaches the rest of the pipeline.
    """
    try:
        return autocrop_pnm(path, trim_px)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot crop %s: %s", path, exc)
        return False


def binarize_image(path: Path, threshold: float) -> bool:
    """Best-effort in-place 1-bit conversion; never fails the page.

    Returns:
        Whether the file was converted. A malformed file is left untouched
        with a warning, so the page still reaches the rest of the pipeline.
    """
    try:
        return binarize_pnm(path, threshold)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot binarize %s: %s", path, exc)
        return False


def image_mean(path: Path) -> float | None:
    """Return the mean brightness (0..1) of an image, or ``None`` if unknown.

    Only raw PNM images are measured. A ``None`` result means blank detection
    must be skipped for this page; the page is then always kept.
    """
    try:
        native = pnm_mean(path)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot parse %s: %s", path, exc)
        return None
    if native is None:
        LOGGER.debug("%s: not a raw PNM; skipping blank detection", path.name)
    return native
