"""Tests for host-side raster deskew, with Tesseract stubbed out.

The measurement is one line of the tool's own output, so the parser and
the angle policy are exercised against recorded strings; the rotation is
exercised for real, because its sign and its format preservation are the
two things that must not drift.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from scanmole.deskew import (
    MAX_PIXELS,
    MIN_ANGLE_DEGREES,
    TIMEOUT_SECONDS,
    Deskewed,
    deskew_page,
    page_angle,
)


def _stub(monkeypatch: pytest.MonkeyPatch, stderr: str, returncode: int = 0) -> None:
    """Answer the measurement with a recorded tool run.

    A real ``run_command`` here is called without ``check``, so it can
    only ever come back as a ``CompletedProcess``; the stub does the
    same, and the module has to decide from the exit code rather than
    from an exception no real call could raise.
    """

    def fake_run(command: list[str], **kwargs: object) -> object:
        assert command[0] == "tesseract" and command[1:3] == ["--psm", "2"]
        assert kwargs["timeout_seconds"] == TIMEOUT_SECONDS
        assert "check" not in kwargs  # the exit code is ours to read
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr("scanmole.deskew.run_command", fake_run)


def _raises(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr("scanmole.deskew.run_command", fake_run)


def _gray(path: Path, width: int = 64, height: int = 48) -> Path:
    """A gray page with one black block well off centre."""
    rows = bytearray(b"\xff" * (width * height))
    for y in range(8, 20):
        for x in range(8, 24):
            rows[y * width + x] = 0
    path.write_bytes(b"P5\n%d %d\n255\n" % (width, height) + bytes(rows))
    return path


def _p4(path: Path, width: int = 64, height: int = 48) -> Path:
    row_bytes = (width + 7) // 8
    raster = bytearray(row_bytes * height)
    for y in range(8, 20):
        for x in range(8, 24):
            raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    path.write_bytes(b"P4\n%d %d\n" % (width, height) + bytes(raster))
    return path


def _color(path: Path, width: int = 64, height: int = 48) -> Path:
    rows = bytearray(b"\xff" * (width * height * 3))
    for y in range(8, 20):
        for x in range(8, 24):
            rows[(y * width + x) * 3 : (y * width + x) * 3 + 3] = b"\x00\x00\x00"
    path.write_bytes(b"P6\n%d %d\n255\n" % (width, height) + bytes(rows))
    return path


def _header(path: Path) -> list[bytes]:
    data = path.read_bytes()
    return data.split(b"\n")[:3]


def _ink_centre(path: Path) -> tuple[float, float]:
    """Centre of mass of the black pixels in a P5 page."""
    data = path.read_bytes()
    tokens = data.split(b"\n", 3)
    width, height = (int(v) for v in tokens[1].split())
    raster = tokens[3]
    dark = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if raster[y * width + x] < 128
    ]
    return (
        sum(x for x, _y in dark) / len(dark),
        sum(y for _x, y in dark) / len(dark),
    )


# --------------------------------------------------------------- parsing


def test_the_angle_comes_back_in_degrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tesseract reports radians; everything downstream works in degrees.
    _stub(monkeypatch, "Orientation: 0\nDeskew angle: 0.0174\n")

    got = page_angle(_gray(tmp_path / "page.pgm"))

    assert got is not None
    assert got == pytest.approx(math.degrees(0.0174))
    assert got == pytest.approx(0.9970, abs=1e-3)


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("Estimating resolution as 300\n", id="no-angle-line"),
        pytest.param("Deskew angle: \n", id="empty"),
        pytest.param("Deskew angle: sideways\n", id="malformed"),
        pytest.param("Deskew angle: nan\n", id="nan"),
        pytest.param("Deskew angle: inf\n", id="infinity"),
        pytest.param("Too few characters. Skipping this page\n", id="empty-page"),
    ],
)
def test_unusable_output_is_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    # Every one of these is a reason to leave the page alone, never to
    # guess an angle for it.
    _stub(monkeypatch, stderr)

    assert page_angle(_gray(tmp_path / "page.pgm")) is None


def test_a_fatal_exit_is_honored_before_the_output_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run that failed can still have printed an angle-shaped line on
    # its way there. The exit code decides, so the line must never be
    # taken for a measurement.
    _stub(
        monkeypatch,
        "Deskew angle: 0.0350\nLeptonica Error in pixRead: pix not read\n",
        returncode=2,
    )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        page_angle(_gray(tmp_path / "page.pgm"))

    assert caught.value.returncode == 2


def test_a_declined_page_has_no_angle_whatever_it_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit 1 is the measured verdict "too sparse to analyse". Whatever
    # stderr happens to carry, that page was not measured.
    _stub(monkeypatch, "Deskew angle: 0.0350\n", returncode=1)

    assert page_angle(_gray(tmp_path / "page.pgm")) is None


def test_a_page_tesseract_declines_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured: a page too sparse to analyse exits 1 with no output at
    # all. That is an ordinary no-evidence result, not a failure, and
    # the raster must come through untouched.
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _stub(monkeypatch, "", returncode=1)

    result = deskew_page(page)

    assert result.outcome is Deskewed.STRAIGHT
    assert page.read_bytes() == original


# ----------------------------------------------------------- angle policy


@pytest.mark.parametrize(
    ("radians", "rotated"),
    [
        pytest.param(math.radians(0.0), False, id="dead-straight"),
        pytest.param(math.radians(0.09), False, id="just-under"),
        pytest.param(math.radians(-0.09), False, id="just-under-negative"),
        pytest.param(math.radians(0.11), True, id="just-over"),
        pytest.param(math.radians(-0.11), True, id="just-over-negative"),
    ],
)
def test_the_minimum_angle_decides_whether_to_resample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, radians: float, rotated: bool
) -> None:
    # Under a tenth of a degree the page is straight enough that
    # resampling would cost sharpness for nothing.
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _stub(monkeypatch, f"Deskew angle: {radians:.6f}\n")

    result = deskew_page(page)

    assert MIN_ANGLE_DEGREES == 0.1
    assert result.outcome is (Deskewed.ROTATED if rotated else Deskewed.STRAIGHT)
    assert (page.read_bytes() != original) is rotated


# ------------------------------------------------------------------- sign


@pytest.mark.parametrize(
    ("radians", "rightwards"),
    [
        pytest.param(math.radians(5.0), False, id="positive-angle"),
        pytest.param(math.radians(-5.0), True, id="negative-angle"),
    ],
)
def test_the_rotation_follows_the_reported_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, radians: float, rightwards: bool
) -> None:
    # Pinned by direction, not by magnitude: the block sits above the
    # page centre, and a positive angle carries it left and down while a
    # negative one carries it right and up. An absolute-value check
    # would pass with the sign reversed; this one cannot.
    page = _gray(tmp_path / "page.pgm")
    before = _ink_centre(page)
    _stub(monkeypatch, f"Deskew angle: {radians:.6f}\n")

    assert deskew_page(page).outcome is Deskewed.ROTATED

    after = _ink_centre(page)
    assert (after[0] > before[0]) is rightwards
    assert (after[0] < before[0]) is not rightwards


# ------------------------------------------------------------ the raster


@pytest.mark.parametrize(
    ("build", "magic", "tokens"),
    [
        pytest.param(_p4, b"P4", 2, id="p4-stays-1-bit"),
        pytest.param(_gray, b"P5", 3, id="p5-stays-gray"),
        pytest.param(_color, b"P6", 3, id="p6-stays-color"),
    ],
)
def test_rotation_preserves_the_raster_family_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build: object,
    magic: bytes,
    tokens: int,
) -> None:
    page = build(tmp_path / "page.pnm")  # type: ignore[operator]
    _stub(monkeypatch, f"Deskew angle: {math.radians(3.0):.6f}\n")

    assert deskew_page(page).outcome is Deskewed.ROTATED

    header = _header(page)
    assert header[0] == magic
    assert header[1] == b"64 48"  # the canvas never changes
    if tokens == 3:
        assert header[2] == b"255"


def test_a_rotated_p4_page_stays_black_and_white(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nearest neighbour, so the result carries no invented gray: every
    # byte is still a bit pattern the format can hold.
    page = _p4(tmp_path / "page.pbm")
    _stub(monkeypatch, f"Deskew angle: {math.radians(4.0):.6f}\n")

    assert deskew_page(page).outcome is Deskewed.ROTATED

    data = page.read_bytes()
    assert data.startswith(b"P4\n64 48\n")
    assert len(data.split(b"\n", 2)[2]) == ((64 + 7) // 8) * 48


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"P5\n8 8\n65535\n" + bytes(128), id="deep-gray"),
        pytest.param(b"P6\n4 4\n65535\n" + bytes(96), id="deep-color"),
    ],
)
def test_deep_color_is_unsupported_rather_than_reduced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> None:
    # Pillow reads a 16-bit P6 as 8-bit and would write it back that way,
    # and a 16-bit P5 opens in a mode whose white is not 255. Handing the
    # request back keeps both from happening quietly.
    page = tmp_path / "deep.pnm"
    page.write_bytes(frame)
    _stub(monkeypatch, f"Deskew angle: {math.radians(3.0):.6f}\n")

    result = deskew_page(page)

    assert result.outcome is Deskewed.UNSUPPORTED
    assert page.read_bytes() == frame


def test_a_frame_too_large_to_rotate_is_unsupported(tmp_path: Path) -> None:
    # Refused on our own terms, before either tool runs, so an oversized
    # unresolved feeder window never reaches Pillow's bomb guard.
    page = tmp_path / "huge.pgm"
    side = int(MAX_PIXELS**0.5) + 10
    page.write_bytes(b"P5\n%d %d\n255\n" % (side, side))

    assert deskew_page(page).outcome is Deskewed.UNSUPPORTED


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"P5\n0 8\n255\n" + bytes(8), id="zero-width"),
        pytest.param(b"P5\n8 0\n255\n" + bytes(8), id="zero-height"),
        pytest.param(b"P5\nwide 8\n255\n" + bytes(8), id="non-numeric"),
        pytest.param(b"P5\n8 8\n0\n" + bytes(64), id="zero-maxval"),
    ],
)
def test_bad_geometry_is_unsupported_before_any_tool_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the tool must not run on a frame we cannot read")

    monkeypatch.setattr("scanmole.deskew.run_command", explode)
    page = tmp_path / "bad.pgm"
    page.write_bytes(frame)

    assert deskew_page(page).outcome is Deskewed.UNSUPPORTED
    assert page.read_bytes() == frame


def test_a_non_pnm_page_is_unsupported(tmp_path: Path) -> None:
    page = tmp_path / "page.png"
    original = b"\x89PNG\r\n\x1a\n" + bytes(16)
    page.write_bytes(original)

    assert deskew_page(page).outcome is Deskewed.UNSUPPORTED
    assert page.read_bytes() == original


# ------------------------------------------------------------- failures


def test_a_tool_that_could_not_read_the_page_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured: an unreadable or corrupt input exits 2 with Leptonica
    # errors. That is a genuine failure, and the pipeline's recovery
    # contract owns it: stop and preserve the pages already acquired.
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _stub(monkeypatch, "Leptonica Error in pixRead: pix not read\n", returncode=2)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        deskew_page(page)

    assert caught.value.returncode == 2
    assert page.read_bytes() == original


def test_a_measurement_timeout_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _raises(monkeypatch, subprocess.TimeoutExpired("tesseract", TIMEOUT_SECONDS))

    with pytest.raises(subprocess.TimeoutExpired):
        deskew_page(page)

    assert page.read_bytes() == original


def test_an_interrupt_during_measurement_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _gray(tmp_path / "page.pgm")
    _raises(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        deskew_page(page)


def test_a_failed_write_leaves_the_acquired_raster_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The frame may be the only copy of the paper: a full disk mid-write
    # must not truncate it, and must leave no staging file behind.
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _stub(monkeypatch, f"Deskew angle: {math.radians(3.0):.6f}\n")
    real_write = Path.write_bytes

    def failing_write(self: Path, data: bytes) -> int:
        if self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    with pytest.raises(OSError, match="No space left"):
        deskew_page(page)

    assert page.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page.pgm"]


def test_a_failed_staging_write_also_reaches_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the write: Pillow stages the rotated raster
    # first. A full disk there must not come back as a quiet "straight".
    page = _gray(tmp_path / "page.pgm")
    original = page.read_bytes()
    _stub(monkeypatch, f"Deskew angle: {math.radians(3.0):.6f}\n")
    real_save = Image.Image.save

    def failing_save(self: object, fp: object, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Image.Image, "save", failing_save)

    with pytest.raises(OSError, match="No space left"):
        deskew_page(page)

    monkeypatch.setattr(Image.Image, "save", real_save)
    assert page.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["page.pgm"]


# ------------------------------------------------- content preservation


def _marked_page(
    path: Path,
    kind: str,
    width: int,
    height: int,
    marks: list[tuple[int, int, int, int]],
) -> Path:
    """A white page of the given PNM kind with black marks (x0, y0, x1, y1)."""
    gray = bytearray(b"\xff" * (width * height))
    for x0, y0, x1, y1 in marks:
        for y in range(y0, min(y1, height)):
            gray[y * width + x0 : y * width + min(x1, width)] = b"\x00" * (
                min(x1, width) - x0
            )
    if kind == "P5":
        path.write_bytes(b"P5\n%d %d\n255\n" % (width, height) + bytes(gray))
    elif kind == "P6":
        path.write_bytes(
            b"P6\n%d %d\n255\n" % (width, height)
            + bytes(v for sample in gray for v in (sample, sample, sample))
        )
    else:
        row_bytes = (width + 7) // 8
        raster = bytearray(row_bytes * height)
        for y in range(height):
            for x in range(width):
                if gray[y * width + x] < 128:
                    raster[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        path.write_bytes(b"P4\n%d %d\n" % (width, height) + bytes(raster))
    return path


def _dark_pixels(path: Path) -> int:
    """Format-neutral count of dark pixels in a raw PNM page."""
    data = path.read_bytes()
    kind = data[:2]
    if kind == b"P4":
        tokens = data.split(b"\n", 2)
        width, height = (int(v) for v in tokens[1].split())
        row_bytes = (width + 7) // 8
        raster = tokens[2][-row_bytes * height :]
        count = int.from_bytes(raster, "big").bit_count()
        if width % 8:
            pad_mask = 0xFF >> (width % 8)
            count -= sum(
                (byte & pad_mask).bit_count()
                for byte in raster[row_bytes - 1 :: row_bytes]
            )
        return count
    tokens = data.split(b"\n", 3)
    width, height = (int(v) for v in tokens[1].split())
    raster = tokens[3][-width * height * (3 if kind == b"P6" else 1) :]
    if kind == b"P6":
        # Luminance, not a single channel: the oracle must see chromatic
        # ink exactly like the evidence the production code measures.
        raster = bytes(
            (raster[i] + 2 * raster[i + 1] + raster[i + 2]) >> 2
            for i in range(0, len(raster), 3)
        )
    return sum(1 for value in raster if value < 128)


def _measured(monkeypatch: pytest.MonkeyPatch, degrees: float) -> None:
    _stub(monkeypatch, f"Deskew angle: {math.radians(degrees)}\n")


def _gray_view(path: Path) -> tuple[bytes, int, int]:
    """The page as one luminance byte per pixel, with its dimensions."""
    data = path.read_bytes()
    kind = data[:2]
    if kind == b"P4":
        tokens = data.split(b"\n", 2)
        width, height = (int(v) for v in tokens[1].split())
        row_bytes = (width + 7) // 8
        raster = tokens[2][-row_bytes * height :]
        gray = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                bit = raster[y * row_bytes + x // 8] & (0x80 >> (x % 8))
                gray[y * width + x] = 0 if bit else 255
        return bytes(gray), width, height
    tokens = data.split(b"\n", 3)
    width, height = (int(v) for v in tokens[1].split())
    raster = tokens[3][-width * height * (3 if kind == b"P6" else 1) :]
    if kind == b"P6":
        raster = bytes(
            (raster[i] + 2 * raster[i + 1] + raster[i + 2]) >> 2
            for i in range(0, len(raster), 3)
        )
    return bytes(raster), width, height


def _rotated_point(
    point: tuple[float, float], canvas: tuple[int, int], degrees: float
) -> tuple[float, float]:
    """Where a source point lands under Pillow's centre rotation.

    An independent restatement of the transform, verified empirically
    against Pillow, so the survival oracle never consults the
    production planning code.
    """
    cx, cy = canvas[0] / 2, canvas[1] / 2
    theta = math.radians(degrees)
    dx, dy = point[0] - cx, point[1] - cy
    return (
        cx + dx * math.cos(theta) + dy * math.sin(theta),
        cy - dx * math.sin(theta) + dy * math.cos(theta),
    )


def _spy_shifts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record the translation actually handed to the real rotation."""
    import scanmole.deskew as deskew_module

    shifts: list[tuple[int, int]] = []
    real = deskew_module._rotate

    def wrapped(
        path: Path, raster: object, degrees: float, shift: tuple[int, int]
    ) -> bool:
        shifts.append(shift)
        return real(path, raster, degrees, shift)  # type: ignore[arg-type]

    monkeypatch.setattr("scanmole.deskew._rotate", wrapped)
    return shifts


_CENTRE_TOLERANCE = 3.0
"""How far the surviving target's centroid may sit from the exact
transformed position: bicubic resampling bleeds under a pixel and the
integer shift rounds by one, so three pixels is generous for a correct
turn and far below any wrong or missing translation."""


def _target_centred_at(
    path: Path,
    target: tuple[int, int, int, int],
    degrees: float,
    shift: tuple[int, int],
) -> bool:
    """Whether the target's ink mass is centred at its exact mapping.

    The expected position is the independent transform oracle: the
    source centre rotated around the canvas centre, plus the translation
    the production code actually applied. The window is barely larger
    than the target itself, so unrelated content elsewhere can never
    satisfy the assertion, and the surviving mass must both be present
    and have its centroid within :data:`_CENTRE_TOLERANCE`.
    """
    gray, width, height = _gray_view(path)
    centre = ((target[0] + target[2]) / 2, (target[1] + target[3]) / 2)
    mapped = _rotated_point(centre, (width, height), degrees) if degrees else centre
    expected = (mapped[0] + shift[0], mapped[1] + shift[1])
    area = (target[2] - target[0]) * (target[3] - target[1])
    half_w = (target[2] - target[0]) / 2 + _CENTRE_TOLERANCE + 4
    half_h = (target[3] - target[1]) / 2 + _CENTRE_TOLERANCE + 4
    x0 = max(0, int(expected[0] - half_w))
    x1 = min(width, int(expected[0] + half_w) + 1)
    y0 = max(0, int(expected[1] - half_h))
    y1 = min(height, int(expected[1] + half_h) + 1)
    dark = [
        (x, y)
        for y in range(y0, y1)
        for x in range(x0, x1)
        if gray[y * width + x] < 128
    ]
    if len(dark) < area * 0.85:
        return False
    centroid = (
        sum(x for x, _y in dark) / len(dark),
        sum(y for _x, y in dark) / len(dark),
    )
    return (
        abs(centroid[0] - expected[0]) <= _CENTRE_TOLERANCE
        and abs(centroid[1] - expected[1]) <= _CENTRE_TOLERANCE
    )


_A5_150 = (874, 1240)
"""A5 at 150 dpi: production-shaped and fast to rotate."""


@pytest.mark.parametrize("angle", [0.5, -0.5, 1.0, -1.0, 1.8, -1.8, 2.0, -2.0])
@pytest.mark.parametrize("kind", ["P4", "P5", "P6"])
def test_corner_targets_survive_the_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, angle: float, kind: str
) -> None:
    # The clipping regression: Pillow rotates inside the original canvas,
    # so a corner mark used to leave it and vanish. Whether the page is
    # turned, shifted or conservatively left as scanned, every meaningful
    # pixel must survive.
    width, height = _A5_150
    target = (0, 0, 20, 20)  # the top-left corner target
    marks = [
        target,
        (width // 3, height // 2, width // 3 + 240, height // 2 + 30),
    ]
    page = _marked_page(tmp_path / "page.pnm", kind, width, height, marks)
    before = _dark_pixels(page)
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, angle)

    result = deskew_page(page)

    # Resampling wobbles the dark count by about twenty pixels; clipping
    # a corner target costs about a hundred at the smallest angle.
    assert before - _dark_pixels(page) <= 48
    # And the target's own mass must sit exactly where the rotation plus
    # the actually applied translation put it: extra dark pixels
    # elsewhere cannot stand in for a lost corner.
    turned = result.outcome is Deskewed.ROTATED
    shift = shifts[-1] if turned else (0, 0)
    assert _target_centred_at(page, target, result.degrees if turned else 0.0, shift)


@pytest.mark.parametrize("angle", [1.8, -1.8])
def test_near_corner_rules_survive_the_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, angle: float
) -> None:
    # Rules three millimetres from every edge of an A4 300 dpi page:
    # genuine border decoration, not a scanner artifact, and exactly what
    # corner clipping eats first (the far corners displace by ~19 px).
    width, height = 2480, 3508
    inset = 35  # 3 mm at 300 dpi
    marks = [
        (inset, inset, width - inset, inset + 12),
        (inset, height - inset - 12, width - inset, height - inset),
        (inset, inset, inset + 12, height - inset),
        (width - inset - 12, inset, width - inset, height - inset),
        (width // 4, height // 2, width // 4 + 480, height // 2 + 60),
    ]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    before = _dark_pixels(page)
    _measured(monkeypatch, angle)

    deskew_page(page)

    assert before - _dark_pixels(page) <= 200  # resampling, never clipping


def test_off_centre_content_survives_and_still_straightens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content crowding one corner leaves room on the other side: the page
    # must still be straightened (by shifting within the same canvas),
    # never scaled and never clipped.
    width, height = _A5_150
    marks = [
        (0, 0, 20, 20),
        (24, 30, 424, 60),
        (24, 90, 424, 120),
    ]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    before = _dark_pixels(page)
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, 2.0)

    result = deskew_page(page)

    assert result.outcome is Deskewed.ROTATED
    header = _header(page)
    assert header[1] == b"%d %d" % (width, height)  # the canvas never grows
    assert before - _dark_pixels(page) <= 48
    assert shifts[-1] != (0, 0)  # the corner demanded a real translation
    assert _target_centred_at(page, (0, 0, 20, 20), result.degrees, shifts[-1])


def test_opposite_corner_content_shifts_the_other_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content crowding the bottom-right corner needs the mirrored
    # translation: both signs of the shift are real, and the target must
    # land at its exact mapping either way.
    width, height = _A5_150
    target = (width - 20, height - 20, width, height)
    marks = [
        target,
        (width - 424, height - 60, width - 24, height - 30),
        (width - 424, height - 120, width - 24, height - 90),
    ]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, 2.0)

    result = deskew_page(page)

    assert result.outcome is Deskewed.ROTATED
    assert shifts[-1] != (0, 0)
    assert shifts[-1][0] <= 0 and shifts[-1][1] <= 0  # the mirrored signs
    assert _target_centred_at(page, target, result.degrees, shifts[-1])


def test_an_unsafe_fixed_canvas_rotation_declines_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Marks in all four corners: no shift can keep them all inside the
    # canvas under the measured turn, and scaling is out of the question,
    # so the page conservatively keeps its skew, untouched.
    width, height = _A5_150
    marks = [
        (0, 0, 20, 20),
        (width - 20, 0, width, 20),
        (0, height - 20, 20, height),
        (width - 20, height - 20, width, height),
        (width // 3, height // 2, width // 3 + 240, height // 2 + 30),
    ]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    before = page.read_bytes()
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, 2.0)

    result = deskew_page(page)

    assert result.outcome is Deskewed.DECLINED
    assert page.read_bytes() == before  # byte-identical preservation
    assert shifts == []  # no rotation and no translation were attempted


def test_a_safe_rotation_stays_byte_identical_to_the_plain_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Centred content with wide margins is the ordinary case, and it must
    # keep producing exactly the raster the unguarded rotation produced.
    width, height = _A5_150
    marks = [(width // 3, height // 3, width // 3 + 240, height // 3 + 200)]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    from PIL import Image as PILImage

    with PILImage.open(page) as image:
        expected = image.rotate(
            2.0, resample=PILImage.Resampling.BICUBIC, fillcolor=255
        )
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, 2.0)

    result = deskew_page(page)

    assert result.outcome is Deskewed.ROTATED
    assert shifts == [(0, 0)]  # the safe path carries no translation
    with PILImage.open(page) as turned:
        assert turned.tobytes() == expected.tobytes()


def test_edge_hugging_artifacts_do_not_hold_the_rotation_hostage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A scanner boundary bar and a trailing-edge shadow band reach the
    # frame border and span an axis: they are not content, so an
    # otherwise safe page still straightens.
    width, height = _A5_150
    marks = [
        (0, 0, 8, height),  # boundary bar on the left frame edge
        (0, height - 10, width, height),  # trailing-edge shadow band
        (width // 3, height // 3, width // 3 + 240, height // 3 + 30),
    ]
    page = _marked_page(tmp_path / "page.pnm", "P5", width, height, marks)
    _measured(monkeypatch, 1.0)

    result = deskew_page(page)

    assert result.outcome is Deskewed.ROTATED


def test_a_dark_green_corner_target_constrains_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Chromatic content is content: a dark-green corner mark is bright in
    # the green channel, so single-channel evidence was blind to it and
    # the turn clipped it. Luminance evidence must keep it aboard.
    width, height = _A5_150
    gray = bytearray(b"\xff" * (width * height))
    for y in range(20):
        for x in range(20):
            gray[y * width + x] = 1  # marker: becomes dark green below
    for y in range(height // 2, height // 2 + 30):
        for x in range(width // 3, width // 3 + 240):
            gray[y * width + x] = 0  # neutral centre block
    p6 = bytearray(b"P6\n%d %d\n255\n" % (width, height))
    for value in gray:
        if value == 1:
            p6 += bytes((0, 150, 0))
        else:
            p6 += bytes((value, value, value))
    page = tmp_path / "page.ppm"
    page.write_bytes(bytes(p6))
    before = _dark_pixels(page)
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, 2.0)

    result = deskew_page(page)

    assert before - _dark_pixels(page) <= 48  # the green mark survived
    turned = result.outcome is Deskewed.ROTATED
    assert _target_centred_at(
        page,
        (0, 0, 20, 20),
        result.degrees if turned else 0.0,
        shifts[-1] if turned else (0, 0),
    )


def test_deep_color_is_rejected_before_evidence_is_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deep P6 is UNSUPPORTED for the host turn outright, and that
    # refusal must come before any evidence inspection: an unsupported
    # evidence conversion is not proof that no content exists, so the
    # ordering is part of the safety contract.
    def exploding_evidence(buffer: bytes, threshold: float) -> bytes | None:
        raise AssertionError("evidence consulted before the depth refusal")

    monkeypatch.setattr("scanmole.deskew.pnm_evidence_mask", exploding_evidence)
    for maxval in (300, 65535):
        pixels = (200).to_bytes(2, "big") * 3 * 16
        page = tmp_path / f"deep-{maxval}.ppm"
        page.write_bytes(b"P6\n4 4\n%d\n" % maxval + pixels)
        before = page.read_bytes()

        result = deskew_page(page)

        assert result.outcome is Deskewed.UNSUPPORTED
        assert page.read_bytes() == before


@pytest.mark.parametrize("kind", ["P4", "P5"])
def test_odd_dimensions_and_p4_padding_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    width, height = 875, 1239  # odd, and 875 % 8 != 0 pads the P4 rows
    marks = [(0, 0, 20, 20), (300, 600, 540, 630)]
    page = _marked_page(tmp_path / "page.pnm", kind, width, height, marks)
    before = _dark_pixels(page)
    shifts = _spy_shifts(monkeypatch)
    _measured(monkeypatch, -1.8)

    result = deskew_page(page)

    assert before - _dark_pixels(page) <= 48
    assert _header(page)[1] == b"%d %d" % (width, height)
    turned = result.outcome is Deskewed.ROTATED
    assert _target_centred_at(
        page,
        (0, 0, 20, 20),
        result.degrees if turned else 0.0,
        shifts[-1] if turned else (0, 0),
    )
