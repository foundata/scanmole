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
