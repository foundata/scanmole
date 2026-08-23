"""End-to-end pipeline test using generated images (no scanner hardware).

Exercises acquisition-from-images, blank dropping and PDF assembly. OCR is left
off so the test needs only ``img2pdf``; it is skipped when that is absent.
"""

from __future__ import annotations

import dataclasses
import glob
import io
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from support.pipeline import (
    _config,
    _gray_page,
)

import scanmole.pipeline as pipeline_module
from scanmole.config import ScanConfig
from scanmole.errors import (
    DeviceError,
    ProcessingError,
    ScanMoleError,
)
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.pipeline import publish_pdf, run_pipeline
from scanmole.scanner import (
    EffectiveSettings,
    ScanResult,
)

pytestmark = pytest.mark.integration


def test_processing_failure_preserves_scanned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        return ScanResult(
            pages=[page],
            settings=EffectiveSettings(source=None, mode=None, resolution=None),
        )

    def failing_build_pdf(pages: object, output: Path, dpi: object) -> None:
        raise ProcessingError("img2pdf failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build_pdf)
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(ProcessingError) as info:
        run_pipeline(config, EventWriter(enabled=False))

    message = info.value.message
    assert "kept in" in message
    work_dir = Path(message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        assert (work_dir / "page_0001.pnm").is_file()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_page_callback_failure_preserves_pages_and_reports_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirrors run_scanimage's contract: a failing page callback terminates the
    # batch and propagates as a ScanMoleError after pages were delivered.
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise ScanMoleError("page processing failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    stream = io.StringIO()
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(config, EventWriter(enabled=True, stream=stream))

    message = info.value.message
    assert "kept in" in message
    work_dir = Path(message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        assert (work_dir / "page_0001.pnm").is_file()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    kinds = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert "scan_done" not in kinds
    assert "done" not in kinds


def test_from_images_failure_does_not_claim_preserved_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _gray_page(tmp_path / "input.pgm")

    def failing_build_pdf(pages: object, output: Path, dpi: object) -> None:
        raise ProcessingError("img2pdf failed: boom")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build_pdf)

    with pytest.raises(ProcessingError) as info:
        run_pipeline(_config((page,), tmp_path / "out.pdf"), EventWriter(enabled=False))

    assert "kept in" not in info.value.message


def _unannounced_scan(pages: list[bytes], error: BaseException):  # type: ignore[no-untyped-def]
    """A scan that writes page files without announcing them, then dies."""

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        for index, data in enumerate(pages, start=1):
            (work_dir / f"page_{index:04d}.pnm").write_bytes(data)
        raise error

    return fake_scan


def _owned_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work_dir = tmp_path / "preserved-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)
    return work_dir


_COMPLETE_FRAME = b"P5\n40 40\n255\n" + bytes([120] * 1600)


def test_unannounced_complete_frame_survives_a_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scanner finished writing the frame but died before --batch-print
    # announced it: no callback ran, yet the file may be the only copy.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME], ScanMoleError("lamp failure")),
    )
    stream = io.StringIO()

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=True, stream=stream),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME
    assert info.value.message.startswith("lamp failure")  # original cause
    # Complete, but nothing processed it, so the directory is kept and the
    # message must not call the frame incomplete.
    assert "completed page file(s)" in info.value.message
    assert "incomplete" not in info.value.message
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert not [e for e in events if e["event"] == "page"]  # never announced


def test_unannounced_frame_survives_an_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME], KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME


def test_completed_pages_are_preserved_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # scanimage renames page_NNNN.pnm.part to page_NNNN.pnm only once the
    # page finished, so every file under that name is a completed frame:
    # preserved exactly, never validated or renamed during the failure.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    second = b"P5\n40 40\n255\n" + bytes([90] * 1600)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([_COMPLETE_FRAME, second], ScanMoleError("feeder jam")),
    )

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (work_dir / "page_0001.pnm").read_bytes() == _COMPLETE_FRAME
    assert (work_dir / "page_0002.pnm").read_bytes() == second
    assert info.value.message.startswith("feeder jam")


def test_a_lone_staging_file_does_not_preserve_the_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupted scanimage leaves its in-progress raster as
    # page_NNNN.pnm.part (measured on sane-backends 1.4.0). That is not a
    # page, so it is not a reason to keep a directory holding nothing
    # else, and no recovery is promised for it.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir_arg: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        (work_dir_arg / "page_0001.pnm.part").write_bytes(_COMPLETE_FRAME[:200])
        raise ScanMoleError("cable pulled")

    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert not work_dir.exists()
    assert "preserved" not in info.value.message


def test_a_recovery_command_never_reaches_a_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Completed pages keep the directory, and an incidental .part may ride
    # along, but the documented rebuild must not pick it up: its glob ends
    # in .pnm, and expanding it must yield the completed page alone.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir_arg: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        page = work_dir_arg / "page_0001.pnm"
        page.write_bytes(_COMPLETE_FRAME)
        assert callable(on_page)
        on_page(page)
        (work_dir_arg / "page_0002.pnm.part").write_bytes(_COMPLETE_FRAME[:200])
        raise ScanMoleError("feeder jam")

    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert work_dir.exists()  # the completed page kept it
    pattern = re.search(r"--from-images (\S+)", info.value.message)
    assert pattern is not None
    expanded = sorted(
        Path(part) for part in glob.glob(shlex.split(pattern.group(1))[0])
    )
    assert expanded == [work_dir / "page_0001.pnm"]


def test_no_artifacts_and_no_callbacks_removes_the_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = _owned_work_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "scanmole.pipeline.scan_to_files",
        _unannounced_scan([], ScanMoleError("no device")),
    )

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert not work_dir.exists()  # nothing to keep: no litter either
    assert "preserved" not in info.value.message


def test_announced_and_unannounced_pages_keep_both_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One page went through its callback, a second completed page was
    # never announced: the established recovery message stays and gains a
    # count of what the command covers but nothing processed.
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir_arg: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=300))
        page = work_dir_arg / "page_0001.pnm"
        page.write_bytes(_COMPLETE_FRAME)
        assert callable(on_page)
        on_page(page)
        (work_dir_arg / "page_0002.pnm").write_bytes(_COMPLETE_FRAME)
        raise ScanMoleError("feeder jam")

    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert f"the 1 scanned page(s) are kept in {work_dir}" in info.value.message
    assert "recover with:" in info.value.message
    # The unannounced page is complete; only its processing was skipped.
    assert "1 further completed page(s)" in info.value.message
    assert "no blank detection or sizing" in info.value.message
    assert "incomplete" not in info.value.message
    assert (work_dir / "page_0002.pnm").read_bytes() == _COMPLETE_FRAME


def test_from_images_failure_never_preserves_a_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # User inputs are not scanner output: a failing run neither claims
    # nor keeps copies of them.
    image = _gray_page(tmp_path / "in.pgm")
    work_dir = _owned_work_dir(tmp_path, monkeypatch)

    def failing_build(pages: object, output: Path, dpi: int) -> None:
        raise ScanMoleError("assembly failed")

    monkeypatch.setattr("scanmole.pipeline.build_pdf", failing_build)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=(image,), output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert not work_dir.exists()
    assert "kept" not in info.value.message
    assert "preserved" not in info.value.message
    assert image.is_file()  # the input itself is untouched


def _failing_scan_at(resolution: int):  # type: ignore[no-untyped-def]
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        assert callable(on_settings)
        on_settings(EffectiveSettings(source=None, mode=None, resolution=resolution))
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise ScanMoleError("feeder jam")

    return fake_scan


def test_recovery_command_names_the_snapped_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rebuild must use the dpi the pages were actually scanned at
    # (after snapping), not the requested value: 300 snapped to 150 here.
    work_dir = tmp_path / "preserved-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _failing_scan_at(150))
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    assert (
        f"--from-images {work_dir}/page_*.pnm -r 150 -o out.pdf" in info.value.message
    )


def test_recovery_command_quotes_shell_sensitive_work_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The message is meant to be pasted into a shell: a work dir with
    # spaces and quotes must come out quoted, with the glob outside the
    # quotes so it still expands.
    work_dir = tmp_path / "scan mole's work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", _failing_scan_at(300))
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)

    with pytest.raises(ScanMoleError) as info:
        run_pipeline(
            _config(images=None, output=tmp_path / "out.pdf"),
            EventWriter(enabled=False),
        )

    quoted = shlex.quote(str(work_dir))
    assert quoted.startswith("'")  # the path genuinely needed quoting
    assert f"--from-images {quoted}/page_*.pnm -r 300 -o out.pdf" in info.value.message


def test_publish_pdf_failure_raises_processing_error(tmp_path: Path) -> None:
    source = tmp_path / "raw.pdf"
    source.write_bytes(b"%PDF-content")

    with pytest.raises(ProcessingError, match="cannot write output"):
        publish_pdf(source, tmp_path / "missing-dir" / "out.pdf")


def test_mid_batch_failure_preserves_sized_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented recovery command uses --from-images, which never crops;
    # pages preserved by a failed run must therefore be sized before the
    # error propagates, or recovery resurrects the full-window frames.
    dpi = 100
    scale = dpi / 25.4
    window = (215.9, 393.7)
    frame_w, frame_h = round(window[0] * scale), round(window[1] * scale)

    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        settings = EffectiveSettings(
            source="ADF Duplex",
            mode="Lineart",
            resolution=dpi,
            window_mm=window,
            duplex=True,
        )
        assert callable(on_settings)
        on_settings(settings)
        row_bytes = (frame_w + 7) // 8
        raster = bytearray(row_bytes * frame_h)
        for y in range(0, round(270 * scale)):
            for index in range(10, 90):
                raster[y * row_bytes + index] = 0xFF
        page = work_dir / "page_0001.pnm"
        page.write_bytes(b"P4\n%d %d\n" % (frame_w, frame_h) + bytes(raster))
        assert callable(on_page)
        on_page(page)
        raise DeviceError("scanner unplugged mid-batch")

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    config = dataclasses.replace(
        _config(images=None, output=tmp_path / "out.pdf"), page_size="auto"
    )

    with pytest.raises(DeviceError) as info:
        run_pipeline(config, EventWriter(enabled=False))

    work_dir = Path(info.value.message.split("kept in ", 1)[1].split(" ", 1)[0])
    try:
        header = (work_dir / "page_0001.pnm").read_bytes().split(b"\n", 2)
        _width, height = map(int, header[1].split())
        assert height == round(297 * scale)  # sized to A4, not the window
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_recovery_sizing_waits_for_the_active_page_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupt mid-batch must not start recovery sizing (or escape to
    # the caller) while a page is still being analyzed: the scanner drains
    # its callbacks first, so the page event always precedes both.
    import threading

    from scanmole.pnm import pnm_mean
    from scanmole.scanner import EffectiveSettings

    order: list[str] = []
    entered = threading.Event()
    work_dirs: list[Path] = []

    def fake_build(
        config: ScanConfig,
        device: str,
        caps: object,
        pattern: str,
        plan: object = None,
    ) -> tuple[list[str], EffectiveSettings]:
        page = pattern.replace("%04d", "0001")
        work_dirs.append(Path(page).parent)
        script = (
            f"printf 'P5\\n4 4\\n255\\n0123456789abcdef' > '{page}'; "
            f"echo '{page}'; exec sleep 30"
        )
        return ["sh", "-c", script], EffectiveSettings(
            source=None, mode=None, resolution=75
        )

    def blocking_mean(page: Path) -> float | None:
        if not entered.is_set():
            entered.set()
            release = threading.Event()
            threading.Timer(0.25, release.set).start()
            release.wait(10)
            order.append("callback-finished")
        return pnm_mean(page)

    real_size = pipeline_module._size_preserved_pages

    def recording_size(*args: object, **kwargs: object) -> None:
        order.append("recovery-sizing")
        real_size(*args, **kwargs)  # type: ignore[arg-type]

    real_wait = subprocess.Popen.wait
    armed = {"value": True}

    def interrupting_wait(
        self: subprocess.Popen[str], timeout: float | None = None
    ) -> int:
        if armed["value"]:
            armed["value"] = False
            assert entered.wait(10)
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr("scanmole.scanner.build_scan_command", fake_build)
    monkeypatch.setattr("scanmole.pipeline.image_mean", blocking_mean)
    monkeypatch.setattr("scanmole.pipeline._size_preserved_pages", recording_size)
    monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)

    stream = io.StringIO()
    config = _config(images=None, output=tmp_path / "out.pdf")
    try:
        with pytest.raises(KeyboardInterrupt):
            run_pipeline(config, EventWriter(enabled=True, stream=stream))
        order.append("raised")

        # The callback finished first, then recovery sizing, then the
        # terminal raise; the page event is on the stream by then.
        assert order == ["callback-finished", "recovery-sizing", "raised"]
        kinds = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
        assert "page" in kinds  # emitted during the drain, before the raise
    finally:
        for work_dir in work_dirs:
            shutil.rmtree(work_dir, ignore_errors=True)


def test_keyboard_interrupt_preserves_scanned_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C mid-batch must not delete the only copy of already-fed paper.
    def fake_scan(
        config: ScanConfig,
        device: str,
        work_dir: Path,
        events: EventWriter,
        on_page: object,
        on_settings: object = None,
    ) -> ScanResult:
        page = _gray_page(work_dir / "page_0001.pnm")
        assert callable(on_page)
        on_page(page)
        raise KeyboardInterrupt

    # Own the work directory instead of diffing a global /tmp glob, which
    # could sweep up (and delete) directories of concurrent suites or of a
    # real scan running on this machine.
    work_dir = tmp_path / "scanmole-interrupt-work"

    def owned_mkdtemp(prefix: str = "") -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr("scanmole.pipeline.require_tools", lambda tools: None)
    monkeypatch.setattr("scanmole.pipeline.pick_default_device", lambda: "test:0")
    monkeypatch.setattr("scanmole.pipeline.scan_to_files", fake_scan)
    monkeypatch.setattr("scanmole.pipeline.tempfile.mkdtemp", owned_mkdtemp)
    config = _config(images=None, output=tmp_path / "out.pdf")

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config, EventWriter(enabled=False))

    assert (work_dir / "page_0001.pnm").is_file()  # preserved for recovery


def test_a_vanished_directory_reports_the_write_failure_not_the_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Publishing into a directory that disappeared must surface as the
    # established ProcessingError. A cleanup error on the way out would
    # replace it with something the caller cannot act on.
    source = tmp_path / "finished.pdf"
    source.write_bytes(b"%PDF-fake")
    output = tmp_path / "gone" / "out.pdf"
    output.parent.mkdir()
    staged: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def vanishing(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | None = None,  # mirrors tempfile's own name
        text: bool = False,
    ) -> tuple[int, str]:
        handle, name = real_mkstemp(suffix, prefix, dir, text)
        staged.append(Path(name))
        return handle, name

    def gone(*_args: object) -> None:
        raise OSError("directory is gone")

    monkeypatch.setattr("scanmole.pipeline.tempfile.mkstemp", vanishing)
    monkeypatch.setattr("scanmole.pipeline.os.replace", gone)

    with pytest.raises(ProcessingError, match="cannot write output"):
        publish_pdf(source, output)

    # The staging file was still cleaned up where that was possible.
    assert staged and not staged[0].exists()


def test_a_cleanup_failure_does_not_mask_the_publishing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "finished.pdf"
    source.write_bytes(b"%PDF-fake")
    output = tmp_path / "out.pdf"

    def failing_replace(*_args: object) -> None:
        raise OSError("write failed")

    def failing_unlink(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("cannot clean up")

    monkeypatch.setattr("scanmole.pipeline.os.replace", failing_replace)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(ProcessingError, match="cannot write output"):
        publish_pdf(source, output)
