"""Blank-page detection via mean image brightness.

Raw PNM images (the format ``scanimage`` writes) are parsed with the standard
library alone. Other formats (possible via ``--from-images``) are not
measured: those inputs are user-curated, so blank detection is skipped for
them instead of pulling in an image library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_WHITESPACE = b" \t\r\n"

_POPCOUNT = bytes(value.bit_count() for value in range(256))
"""Translate table turning each raster byte into its number of set bits."""


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

    Already-1-bit (``P4``) and non-PNM files are left alone: 1-bit padding is
    indistinguishable from the page's own white margin, so native-lineart
    devices rely on hardware lower-edge detection instead (``--ald``, see the
    scan command assembly).

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


_INK_CUTOFF = 0.5
"""Gray fraction below which a pixel counts as ink for content detection."""

_CONTENT_RUN_ROWS = 3
"""Consecutive inked rows required before a row edge counts as content."""

_CONTENT_RUN_BINS = 2
"""Consecutive inked 8-px column bins required (kills 1-2 px roller streaks)."""

_REACH_RUN = 2
"""Consecutive units required for the permissive reach envelope."""


@dataclass(frozen=True)
class ContentStats:
    """Where the printed content of a frame sits.

    Two envelopes with different jobs: ``bbox`` is the robust content box for
    size inference (heavily eroded, so specks and streaks cannot inflate the
    chosen paper size), ``reach`` is a permissive superset for crop safety
    (lightly eroded, so faint but real content such as lone page numbers and
    signature lines is never cut off, at the price of occasionally oversized
    pages). Any crop must contain ``reach``; only ``bbox`` picks the size.

    Attributes:
        frame: Frame width and height in pixels.
        bbox: Robust content box ``(x0, y0, x1, y1)`` with exclusive ends,
            ``x`` on an 8-px grid, or ``None`` when the frame holds no
            plausible content.
        reach: Permissive content envelope, same form; a superset of ``bbox``
            when both exist. ``None`` only when nothing at all was found.
        mean: Brightness proxy (0..1, ``1.0`` = empty) measured inside
            ``bbox`` only, so surrounding padding cannot mask a sparse page.
    """

    frame: tuple[int, int]
    bbox: tuple[int, int, int, int] | None
    reach: tuple[int, int, int, int] | None
    mean: float


def _eroded_span(flags: list[bool], run: int) -> tuple[int, int] | None:
    """First and last position of ``run`` consecutive set flags, or ``None``.

    Returns the span as ``(start, end)`` with an exclusive end.
    """
    count = len(flags) - run + 1
    first = next((i for i in range(count) if all(flags[i : i + run])), None)
    if first is None:
        return None
    last = next(i for i in range(count - 1, -1, -1) if all(flags[i : i + run]))
    return first, last + run


def pnm_content_stats(path: Path, *, min_ink_px: int) -> ContentStats | None:
    """Measure the content bounding box of a raw PNM frame.

    Unlike :func:`autocrop_pnm` this does not look for the paper's edges (on
    white-backed frames there are none to find); it finds the printed content
    instead, robustly against specks and hairline roller streaks: a row only
    counts as content with at least ``min_ink_px`` inked pixels and
    :data:`_CONTENT_RUN_ROWS` inked neighbors, columns are judged in 8-px bins
    with :data:`_CONTENT_RUN_BINS` inked neighbor bins.

    A dark surround (backing, test pattern) needs no special casing here: it
    reads as ink, inflates the box toward the full frame, and the resulting
    crop degenerates into a no-op at the caller.

    Args:
        path: Image file to inspect.
        min_ink_px: Inked pixels a full row needs to count as content.

    Returns:
        The measurements, or ``None`` when the file is not a raw PNM.

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"4", b"5", b"6"):
        return None
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 2 if kind == b"4" else 3)
    try:
        width, height = int(tokens[0]), int(tokens[1])
        maxval = 1 if kind == b"4" else int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")

    # Reduce every variant to one byte per unit whose value is "inked pixels":
    # P4 bytes cover 8 columns via a popcount table, gray/color pixels map to
    # 0/1 through a threshold table (green channel and high bytes as usual).
    if kind == b"4":
        row_bytes = (width + 7) // 8
        if len(buffer) - offset < row_bytes * height:
            raise ValueError("truncated PNM raster")
        raster = bytearray(buffer[offset : offset + row_bytes * height])
        if width % 8:  # row padding bits are don't-care; keep them out
            pad_mask = 0xFF ^ (0xFF >> (width % 8))
            raster[row_bytes - 1 :: row_bytes] = bytes(
                byte & pad_mask for byte in raster[row_bytes - 1 :: row_bytes]
            )
        ink = bytes(raster).translate(_POPCOUNT)
        units = row_bytes  # units per row; one unit = 8 columns
    else:
        channels = 3 if kind == b"6" else 1
        deep = maxval > 255
        row_bytes = width * channels * (2 if deep else 1)
        if len(buffer) - offset < row_bytes * height:
            raise ValueError("truncated PNM raster")
        gray = buffer[offset : offset + row_bytes * height]
        if deep:
            gray, maxval = gray[0::2], maxval >> 8
        if channels == 3:
            gray = gray[1::3]
        cut = min(maxval, max(1, round(_INK_CUTOFF * maxval)))
        ink = gray.translate(bytes(1 if value < cut else 0 for value in range(256)))
        units = width  # one unit = 1 column

    row_ink = [sum(ink[row * units : (row + 1) * units]) for row in range(height)]

    # Column profile in 8-px bins over a row subsample (strided C-speed sums).
    step = max(1, height // 768)
    sampled = b"".join(
        ink[row * units : (row + 1) * units] for row in range(0, height, step)
    )
    sampled_rows = len(sampled) // units
    bins = (width + 7) // 8
    if kind == b"4":
        bin_ink = [sum(sampled[index::units]) for index in range(units)]
    else:
        bin_ink = [
            sum(
                sum(sampled[column::units])
                for column in range(index * 8, min(index * 8 + 8, width))
            )
            for index in range(bins)
        ]

    row_min = max(2, min_ink_px)
    bin_min = max(2, round(min_ink_px * sampled_rows / height))
    row_span = _eroded_span([count >= row_min for count in row_ink], _CONTENT_RUN_ROWS)
    bin_span = _eroded_span([count >= bin_min for count in bin_ink], _CONTENT_RUN_BINS)

    # Permissive envelope: much lower ink thresholds, shorter runs. Faint but
    # real content (page numbers, signature lines, hairline rules) lands here
    # even when it is too weak to be sizing evidence. Hairline streaks stay
    # out: a 1-2 px vertical streak adds at most 2 ink per row and fills only
    # a single column bin, under the row floor of 3 and the 2-bin run.
    reach_row_min = max(3, min_ink_px // 4)
    reach_bin_min = max(1, bin_min // 2)
    reach_rows = _eroded_span([count >= reach_row_min for count in row_ink], _REACH_RUN)
    reach_bins = _eroded_span([count >= reach_bin_min for count in bin_ink], _REACH_RUN)

    bbox: tuple[int, int, int, int] | None = None
    mean = 1.0
    if row_span is not None and bin_span is not None:
        y0, y1 = row_span
        x0, x1 = bin_span[0] * 8, min(bin_span[1] * 8, width)
        if x1 - x0 >= _MIN_PAPER_PX and y1 - y0 >= _MIN_PAPER_PX:
            bbox = (x0, y0, x1, y1)
            # Ink inside the box only, so an eroded streak elsewhere cannot
            # skew the blank verdict.
            if kind == b"4":
                start, stop = x0 // 8, (x1 + 7) // 8
            else:
                start, stop = x0, x1
            box_ink = sum(
                sum(ink[row * units + start : row * units + stop])
                for row in range(y0, y1)
            )
            area = (x1 - x0) * (y1 - y0)
            mean = min(1.0, max(0.0, 1.0 - box_ink / area))

    reach: tuple[int, int, int, int] | None = None
    if reach_rows is not None and reach_bins is not None:
        reach = (
            reach_bins[0] * 8,
            reach_rows[0],
            min(reach_bins[1] * 8, width),
            reach_rows[1],
        )
    if bbox is not None:  # the permissive envelope must contain the robust box
        if reach is None:
            reach = bbox
        else:
            reach = (
                min(reach[0], bbox[0]),
                min(reach[1], bbox[1]),
                max(reach[2], bbox[2]),
                max(reach[3], bbox[3]),
            )
    return ContentStats(frame=(width, height), bbox=bbox, reach=reach, mean=mean)


def image_content_stats(path: Path, *, min_ink_px: int) -> ContentStats | None:
    """Best-effort :func:`pnm_content_stats`; never fails the page.

    Returns:
        The measurements, or ``None`` for non-PNM or malformed files (with a
        warning), letting the caller fall back to the plain path.
    """
    try:
        return pnm_content_stats(path, min_ink_px=min_ink_px)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot measure %s: %s", path, exc)
        return None


def crop_pnm(path: Path, box: tuple[int, int, int, int]) -> bool:
    """Crop a raw PNM to ``box`` (``x0, y0, x1, y1``, exclusive ends), in place.

    1-bit ``P4`` files are cropped from a byte-aligned origin: ``x0`` is
    aligned down to the previous multiple of 8 (growing the box by up to 7 px
    on the left, 3.6 mm even at 50 dpi) while the right edge and therefore
    the requested width stay exact, instead of bit-shifting the raster. Any
    padding bits in the resulting last row byte are cleared to white.

    Returns:
        Whether the file was rewritten. ``False`` means the box is not a real
        subset of the frame (not a raw PNM, degenerate, or the full frame).

    Raises:
        ValueError: If the file starts as a PNM but is malformed or truncated.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"4", b"5", b"6"):
        return False
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 2 if kind == b"4" else 3)
    try:
        width, height = int(tokens[0]), int(tokens[1])
        maxval = 1 if kind == b"4" else int(tokens[2])
    except ValueError as exc:
        raise ValueError("bad PNM header") from exc
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")
    if not 0 < maxval < 65536:
        raise ValueError("bad PNM maxval")

    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if kind == b"4":
        x0 = (x0 // 8) * 8  # aligned origin; the right edge stays exact
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return False
    if (x0, y0, x1, y1) == (0, 0, width, height):
        return False

    if kind == b"4":
        row_bytes = (width + 7) // 8
        if len(buffer) - offset < row_bytes * height:
            raise ValueError("truncated PNM raster")
        raster = buffer[offset : offset + row_bytes * height]
        new_width = x1 - x0
        start, stop = x0 // 8, (x1 + 7) // 8
        rows = [
            raster[row * row_bytes + start : row * row_bytes + stop]
            for row in range(y0, y1)
        ]
        if new_width % 8:  # clear don't-care padding bits to white
            keep = (0xFF << (8 - new_width % 8)) & 0xFF
            rows = [row[:-1] + bytes((row[-1] & keep,)) for row in rows]
        header = b"P4\n%d %d\n" % (new_width, y1 - y0)
        path.write_bytes(header + b"".join(rows))
        return True

    channels = 3 if kind == b"6" else 1
    pixel_bytes = channels * (2 if maxval > 255 else 1)
    row_bytes = width * pixel_bytes
    if len(buffer) - offset < row_bytes * height:
        raise ValueError("truncated PNM raster")
    raster = buffer[offset : offset + row_bytes * height]
    start, stop = x0 * pixel_bytes, x1 * pixel_bytes
    data = b"".join(
        raster[row * row_bytes + start : row * row_bytes + stop]
        for row in range(y0, y1)
    )
    header = b"%s\n%d %d\n%d\n" % (
        b"P6" if kind == b"6" else b"P5",
        x1 - x0,
        y1 - y0,
        maxval,
    )
    path.write_bytes(header + data)
    return True


def crop_image(path: Path, box: tuple[int, int, int, int]) -> bool:
    """Best-effort in-place crop to ``box``; never fails the page.

    Returns:
        Whether the file was cropped. A malformed file is left untouched with
        a warning, so the page still reaches the rest of the pipeline.
    """
    try:
        return crop_pnm(path, box)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot crop %s: %s", path, exc)
        return False
