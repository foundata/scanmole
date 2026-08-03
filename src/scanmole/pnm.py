"""Blank-page detection via mean image brightness.

Raw PNM images (the format ``scanimage`` writes) are parsed with the standard
library alone. Any other image format falls back to ImageMagick when it is
available.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from scanmole.external import IMAGE_TIMEOUT_SECONDS, run_command

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
        ValueError: If the file starts as a PNM but the header is malformed.
    """
    buffer = path.read_bytes()
    if len(buffer) < 8 or buffer[:1] != b"P" or buffer[1:2] not in (b"4", b"5", b"6"):
        return None
    kind = buffer[1:2]
    tokens, offset = _read_header(buffer, 2 if kind == b"4" else 3)
    width, height = int(tokens[0]), int(tokens[1])
    if width <= 0 or height <= 0:
        raise ValueError("bad PNM dimensions")

    if kind == b"4":  # 1 bit per pixel, rows byte-padded, a set bit is black
        row_bytes = (width + 7) // 8
        data = buffer[offset : offset + row_bytes * height]
        black = int.from_bytes(data, "big").bit_count()
        return max(0.0, 1.0 - black / (width * height))

    maxval = int(tokens[2])
    channels = 3 if kind == b"6" else 1
    deep = maxval > 255
    row_bytes = width * channels * (2 if deep else 1)
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


def _magick_mean(path: Path) -> float | None:
    """Return the mean brightness of any image via ImageMagick, or ``None``."""
    tool = shutil.which("magick") or shutil.which("convert")
    if tool is None:
        LOGGER.warning(
            "%s: not a PNM and ImageMagick is not installed -- skipping blank "
            "detection for this page",
            path.name,
        )
        return None
    result = run_command(
        [tool, f"{path}[0]", "-colorspace", "gray", "-format", "%[fx:mean]", "info:"],
        timeout_seconds=IMAGE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        LOGGER.warning(
            "ImageMagick could not analyze %s: %s", path, result.stderr.strip()
        )
        return None
    try:
        return float(result.stdout.split()[0])
    except (ValueError, IndexError):
        return None


def image_mean(path: Path) -> float | None:
    """Return the mean brightness (0..1) of any image, or ``None`` if unknown.

    Raw PNM images are measured natively; other formats use ImageMagick when it
    is installed. A ``None`` result means blank detection must be skipped for
    this page.
    """
    try:
        native = pnm_mean(path)
    except (ValueError, OSError) as exc:
        LOGGER.warning("cannot parse %s: %s", path, exc)
        return None
    if native is not None:
        return native
    return _magick_mean(path)
