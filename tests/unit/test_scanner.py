"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError, ProcessingError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.scanner import (
    EffectiveSettings,
    build_scan_command,
    run_scanimage,
    scan_to_files,
)
from scanmole.sensors import SensorSnapshot
from scanmole.sheetflow import CollectCommands, PageOrigin


def _config(**overrides: object) -> ScanConfig:
    values: dict[str, object] = {
        "device": None,
        "source": "adf-duplex",
        "mode": "lineart",
        "resolution": 300,
        "page_size": "a4",
        "despeckle": 1,
        "deskew": False,
        "crop": False,
        "ocr": False,
        "lang": "deu",
        "rotate_pages": True,
        "optimize": 1,
        "pdfa": False,
        "blank_threshold": 0.995,
        "keep_blanks": False,
        "from_images": None,
        "keep_images": None,
        "output": Path("out.pdf"),
    }
    values.update(overrides)
    return ScanConfig(**values)  # type: ignore[arg-type]


def test_run_scanimage_reports_pages_printed_on_stdout(tmp_path: Path) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    exit_code, stderr = run_scanimage(
        [
            "sh",
            "-c",
            f"echo '{first}'; echo 'Scanned page 1.' >&2; echo '{second}'; exit 7",
        ],
        seen.append,
    )

    assert exit_code == 7
    assert seen == [first, second]
    assert "Scanned page 1." in stderr


def test_run_scanimage_waits_for_slow_page_callbacks(tmp_path: Path) -> None:
    # The process can exit long before the callbacks finish; returning while
    # one still runs would race the caller's post-batch logic against a page
    # that is still being analyzed. All callbacks must complete first.
    import time

    pages = [tmp_path / f"page_{n:04d}.pnm" for n in range(1, 4)]
    handled: list[Path] = []

    def slow_callback(page: Path) -> None:
        time.sleep(0.2)
        handled.append(page)

    script = "; ".join(f"echo '{page}'" for page in pages)
    run_scanimage(["sh", "-c", script], slow_callback)

    assert handled == pages  # complete and in order at the moment of return


def test_run_scanimage_ignores_non_page_stdout_lines(tmp_path: Path) -> None:
    seen: list[Path] = []

    run_scanimage(["sh", "-c", "echo 'not a page'; echo ''"], seen.append)

    assert seen == []


def test_run_scanimage_propagates_page_callback_failures(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"

    def failing_callback(path: Path) -> None:
        raise RuntimeError("callback exploded")

    # The sleep would stall the test for 30s if the failing callback did not
    # terminate the subprocess promptly.
    with pytest.raises(ScanMoleError, match="callback exploded") as info:
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], failing_callback)

    assert isinstance(info.value.__cause__, RuntimeError)


def test_run_scanimage_propagates_domain_errors_unwrapped(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"
    error = ProcessingError("event stream gone")

    def failing_callback(path: Path) -> None:
        raise error

    with pytest.raises(ProcessingError) as info:
        run_scanimage(["sh", "-c", f"echo '{page}'"], failing_callback)

    assert info.value is error


def test_run_scanimage_stops_delivering_after_a_callback_failure(
    tmp_path: Path,
) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    def failing_callback(path: Path) -> None:
        seen.append(path)
        raise RuntimeError("boom")

    with pytest.raises(ScanMoleError):
        run_scanimage(
            ["sh", "-c", f"echo '{first}'; echo '{second}'"], failing_callback
        )

    assert seen == [first]


class _BlockingCallback:
    """A page callback that blocks until released, recording its lifecycle."""

    def __init__(self, hold_seconds: float = 0.25) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.seen: list[str] = []
        self._hold_seconds = hold_seconds

    def __call__(self, page: Path) -> None:
        self.seen.append(page.name)
        if not self.entered.is_set():
            self.entered.set()
            # Release shortly after: long enough that a premature raise is
            # provably premature, short enough to keep the test fast.
            threading.Timer(self._hold_seconds, self.release.set).start()
            self.release.wait(10)
            self.finished.set()


def _interrupting_wait(
    monkeypatch: pytest.MonkeyPatch, trigger: threading.Event
) -> None:
    """Deliver KeyboardInterrupt inside the first ``process.wait()`` call.

    Deterministic stand-in for a SIGINT arriving while the batch runs: the
    scan-timeout wait blocks until the callback has provably entered, then
    raises. Later ``wait()`` calls (the reap) behave normally.
    """
    real_wait = subprocess.Popen.wait
    state = {"armed": True}

    def wait(self: subprocess.Popen[str], timeout: float | None = None) -> int:
        if state["armed"]:
            state["armed"] = False
            assert trigger.wait(10)
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)


def _no_scanner_threads() -> bool:
    return not [
        thread for thread in threading.enumerate() if "scanmole-" in thread.name
    ]


def test_interrupt_waits_for_the_active_callback_and_buffered_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression this change is about: a SIGINT (KeyboardInterrupt)
    # during the batch must not escape run_scanimage while an entered
    # callback still runs, and announcements already in the pipe must be
    # delivered, in order, before the interrupt propagates.
    pages = [tmp_path / f"page_{n:04d}.pnm" for n in (1, 2, 3)]
    callback = _BlockingCallback()
    _interrupting_wait(monkeypatch, callback.entered)
    announce = "; ".join(f"echo '{page}'" for page in pages)

    with pytest.raises(KeyboardInterrupt):
        run_scanimage(["sh", "-c", f"{announce}; sleep 30"], callback)

    assert callback.finished.is_set()  # the entered callback ran to its end
    assert callback.seen == [page.name for page in pages]  # buffered, in order
    assert _no_scanner_threads()  # both readers finished before the raise


def test_timeout_waits_for_the_active_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.scanner.SCAN_TIMEOUT_SECONDS", 0.2)
    page = tmp_path / "page_0001.pnm"
    callback = _BlockingCallback()

    with pytest.raises(DeviceError, match="timed out"):
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], callback)

    assert callback.finished.is_set()
    assert _no_scanner_threads()


def test_interrupt_during_the_drain_is_raised_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Normal completion, but the interrupt lands in the reader join: it
    # must be recorded, the drain must continue, and it must be raised
    # only after every callback and reader finished.
    page = tmp_path / "page_0001.pnm"
    callback = _BlockingCallback()
    real_join = threading.Thread.join
    armed = {"value": True}

    def interrupting_join(self: threading.Thread, timeout: float | None = None) -> None:
        if armed["value"]:
            armed["value"] = False
            raise KeyboardInterrupt
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", interrupting_join)

    with pytest.raises(KeyboardInterrupt):
        run_scanimage(["sh", "-c", f"echo '{page}'"], callback)

    assert callback.finished.is_set()
    assert _no_scanner_threads()


def test_callback_failure_keeps_precedence_over_a_drain_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The failure event already ended the wait when the interrupt lands
    # in the reader join: the diagnosis must stay the processing failure,
    # not the later Ctrl-C.
    page = tmp_path / "page_0001.pnm"

    def failing_callback(path: Path) -> None:
        raise RuntimeError("callback exploded")

    real_join = threading.Thread.join
    armed = {"value": True}

    def interrupting_join(self: threading.Thread, timeout: float | None = None) -> None:
        if armed["value"]:
            armed["value"] = False
            raise KeyboardInterrupt
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", interrupting_join)

    with pytest.raises(ScanMoleError, match="callback exploded") as info:
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], failing_callback)

    assert isinstance(info.value.__cause__, RuntimeError)
    assert _no_scanner_threads()  # the drain still ran to its end


def test_interrupt_after_a_drain_callback_failure_keeps_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child exited with a buffered announcement; the callback fails
    # during the drain, and only then an interrupt lands in a later join:
    # the earlier failure must stay the diagnosis.
    page = tmp_path / "page_0001.pnm"
    in_drain = threading.Event()

    def failing_callback(path: Path) -> None:
        assert in_drain.wait(10)  # fail only once the drain has begun
        raise RuntimeError("late boom")

    real_join = threading.Thread.join
    calls = {"count": 0}

    def sequenced_join(self: threading.Thread, timeout: float | None = None) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt  # lands after the failure was recorded
        if calls["count"] == 1:
            in_drain.set()  # the reader now fails and records the cause
        real_join(self, timeout)  # a retried step joins normally

    monkeypatch.setattr(threading.Thread, "join", sequenced_join)

    with pytest.raises(ScanMoleError, match="late boom") as info:
        run_scanimage(["sh", "-c", f"echo '{page}'"], failing_callback)

    assert isinstance(info.value.__cause__, RuntimeError)
    assert _no_scanner_threads()  # the drain still ran to its end


def test_interrupt_before_a_drain_failure_keeps_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reverse order: the interrupt terminated the wait first, and a
    # callback failing on a buffered page during the drain must not
    # replace it.
    pages = [tmp_path / f"page_{n:04d}.pnm" for n in (1, 2)]
    entered = threading.Event()

    def failing_late(path: Path) -> None:
        entered.set()
        if path.name == "page_0002.pnm":
            raise RuntimeError("late failure")

    _interrupting_wait(monkeypatch, entered)
    announce = "; ".join(f"echo '{page}'" for page in pages)

    with pytest.raises(KeyboardInterrupt):
        run_scanimage(["sh", "-c", f"{announce}; sleep 30"], failing_late)

    assert _no_scanner_threads()


def _term_ignoring_announcer(page: Path) -> list[str]:
    """A fake scanimage that announces one page, ignores TERM and lingers."""
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"print({str(page)!r}, flush=True)\n"
        "time.sleep(300)\n"
    )
    return [sys.executable, "-u", "-c", code]


def test_callback_failure_aborts_a_term_ignoring_scan_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reader records the failure and TERMs the child, but a child that
    # ignores TERM must not leave the controller inside the hour-long scan
    # wait; the KILL escalation applies and the *callback* failure is what
    # gets reported, not a bogus scan timeout.
    monkeypatch.setattr("scanmole.scanner.SCAN_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr("scanmole.scanner.REAP_GRACE_SECONDS", 0.3, raising=False)
    page = tmp_path / "page_0001.pnm"
    error = RuntimeError("boom")

    def failing_callback(path: Path) -> None:
        raise error

    started = time.monotonic()
    with pytest.raises(ScanMoleError, match="page processing failed: boom") as info:
        run_scanimage(_term_ignoring_announcer(page), failing_callback)

    assert time.monotonic() - started < 5.0  # never the 20 s scan timeout
    assert info.value.__cause__ is error
    assert _no_scanner_threads()


def test_scan_timeout_keeps_precedence_over_a_later_callback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The callback blocks past the scan deadline and only fails afterwards
    # (released by the timeout path's kill): the genuine acquisition
    # timeout fired first and must stay the reported cause.
    monkeypatch.setattr("scanmole.scanner.SCAN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("scanmole.scanner.REAP_GRACE_SECONDS", 0.3, raising=False)
    page = tmp_path / "page_0001.pnm"
    release = threading.Event()
    real_kill = subprocess.Popen.kill

    def releasing_kill(self: subprocess.Popen[str]) -> None:
        release.set()
        real_kill(self)

    monkeypatch.setattr(subprocess.Popen, "kill", releasing_kill)
    delivered = threading.Event()

    def late_failing_callback(path: Path) -> None:
        delivered.set()
        assert release.wait(10)
        raise RuntimeError("late boom")

    with pytest.raises(DeviceError, match="timed out"):
        run_scanimage(_term_ignoring_announcer(page), late_failing_callback)

    assert delivered.is_set()  # the failure really happened, after the timeout
    assert _no_scanner_threads()


def test_reader_startup_failure_still_reaps_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the second reader cannot start, the drain must not retry joining
    # a never-started thread forever: reap the child, join what started,
    # close the pipes and raise the startup failure.
    page = tmp_path / "page_0001.pnm"
    real_start = threading.Thread.start

    def failing_start(self: threading.Thread) -> None:
        if self.name == "scanmole-stderr-reader":
            raise RuntimeError("no more threads")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    with pytest.raises(RuntimeError, match="no more threads"):
        run_scanimage(["sh", "-c", f"echo '{page}'; exec sleep 30"], lambda p: None)

    assert _no_scanner_threads()


def test_persistent_close_failure_is_recorded_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cleanup step that always fails must be recorded once as the
    # terminating cause, never spun on: the run ends with the failure
    # instead of hanging.
    page = tmp_path / "page_0001.pnm"

    def failing_close(stream: object) -> None:
        if hasattr(stream, "close"):
            stream.close()  # really close: no fd may leak to the GC
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr("scanmole.scanner._close_stream", failing_close)
    seen: list[Path] = []

    with pytest.raises(OSError, match="Bad file descriptor"):
        run_scanimage(["sh", "-c", f"echo '{page}'"], seen.append)

    assert seen == [page]  # the batch itself completed before cleanup failed
    assert _no_scanner_threads()


def test_build_scan_command_uses_batch_print(tmp_path: Path) -> None:
    command, effective = build_scan_command(
        _config(), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-print" in command
    # An empty listing proves nothing, so the duplex request stands: the
    # pipeline pairs the frames it would then get exactly the same way.
    assert effective == EffectiveSettings(
        source=None, mode=None, resolution=None, duplex=True
    )


def test_build_scan_command_auto_size_requests_the_full_window(
    tmp_path: Path,
) -> None:
    caps = {
        "x": Capability(kind="range", minimum=0.0, maximum=215.9),
        "y": Capability(kind="range", minimum=0.0, maximum=3098.8),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "215.9"
    assert command[command.index("-y") + 1] == "3098.8"


def test_build_scan_command_auto_size_clamps_the_area_to_the_page_limits(
    tmp_path: Path,
) -> None:
    # fujitsu backends advertise the -x/-y ranges of the *current* window
    # (A4 height) and only extend them once --page-width/--page-height are
    # raised, so the true limits are the page geometry maxima.
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "221.121"
    assert command[command.index("-y") + 1] == "876.695"
    assert command.index("--page-height") < command.index("-y")


def test_build_scan_command_auto_size_enables_lower_edge_detection(
    tmp_path: Path,
) -> None:
    caps = {"ald": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    no_ald_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--ald=yes" in auto_command
    assert "--ald=yes" not in fixed_command
    assert "--ald=yes" not in no_ald_command


def test_build_scan_command_fixed_size_may_exceed_the_bare_axis_range(
    tmp_path: Path,
) -> None:
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="legal"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--page-height") + 1] == "355.6"
    assert command[command.index("-y") + 1] == "355.6"


def test_build_scan_command_auto_size_enables_hardware_adf_cropping(
    tmp_path: Path,
) -> None:
    # epsonds' "ADF auto cropping": white-backing devices cannot be cropped
    # in software, so auto page size hands the job to the hardware.
    caps = {"adf-crp": Capability(kind="bool"), "adf-skew": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4", deskew=True),
        "test:0",
        caps,
        str(tmp_path / "p_%04d.pnm"),
    )

    assert "--adf-crp=yes" in auto_command
    assert "--adf-crp=yes" not in fixed_command
    assert "--adf-skew=no" in auto_command
    assert "--adf-skew=yes" in fixed_command


def test_build_scan_command_degraded_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    # Flatbed-only device, duplex requested: the mapper degrades to the
    # flatbed, and --batch-count=1 must key on that mapped source or the
    # batch loops forever (a flatbed never reports "feeder empty").
    caps = {"source": Capability(kind="enum", choices=["Flatbed"])}

    command, effective = build_scan_command(
        _config(source="adf-duplex"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert effective.source == "Flatbed"
    assert command[command.index("--source") + 1] == "Flatbed"
    assert "--batch-count=1" in command


def test_build_scan_command_requested_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    command, _effective = build_scan_command(
        _config(source="flatbed"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-count=1" in command


_FEEDER_SOURCES = Capability(
    kind="enum", choices=["ADF Front", "ADF Back", "ADF Duplex"]
)


def test_single_sheet_on_a_duplex_source_limits_to_two_frames(
    tmp_path: Path,
) -> None:
    # One physical sheet is two frames on a duplex source; --batch-count=1
    # would stop after the front side and split the sheet.
    caps = {"source": _FEEDER_SOURCES}

    command, effective = build_scan_command(
        _config(source="adf-duplex", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert effective.source == "ADF Duplex"
    assert "--batch-count=2" in command
    assert "--batch-count=1" not in command


@pytest.mark.parametrize("source", ["adf", "adf-back"])
def test_single_sheet_on_a_simplex_source_limits_to_one_frame(
    tmp_path: Path, source: str
) -> None:
    caps = {"source": _FEEDER_SOURCES}

    command, _effective = build_scan_command(
        _config(source=source, sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert command.count("--batch-count=1") == 1
    assert "--batch-count=2" not in command


def test_single_sheet_uses_the_degraded_effective_source(tmp_path: Path) -> None:
    # A simplex request on a duplex-only feeder runs duplex; the frame
    # limit must follow what the scanner will actually deliver, not the
    # request, or the sheet's back side is cut off.
    caps = {"source": Capability(kind="enum", choices=["ADF Duplex"])}

    command, effective = build_scan_command(
        _config(source="adf", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert effective.source == "ADF Duplex"
    assert "--batch-count=2" in command


def test_single_sheet_on_a_flatbed_keeps_one_batch_count(tmp_path: Path) -> None:
    caps = {"source": Capability(kind="enum", choices=["Flatbed"])}

    command, _effective = build_scan_command(
        _config(source="flatbed", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert command.count("--batch-count=1") == 1


def test_single_sheet_refuses_unknown_source_evidence(tmp_path: Path) -> None:
    # Without a conclusively negotiated source ScanMole cannot know whether
    # one physical sheet is one frame or two; refuse before feeding paper.
    with pytest.raises(DeviceError, match="single"):
        build_scan_command(
            _config(source="adf-duplex", sheet_flow="single"),
            "test:0",
            {},
            str(tmp_path / "page_%04d.pnm"),
        )


def test_stack_flow_keeps_the_existing_command(tmp_path: Path) -> None:
    # The default flow must stay byte-for-byte what it was before sheet
    # flows existed: no frame limit on a feeder, --batch-count=1 on the
    # flatbed only.
    caps = {"source": _FEEDER_SOURCES}

    command, _effective = build_scan_command(
        _config(source="adf-duplex", sheet_flow="stack"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert not any(part.startswith("--batch-count") for part in command)
    assert not any(part.startswith("--batch-start") for part in command)


def test_batch_start_is_emitted_for_continuation_segments(tmp_path: Path) -> None:
    caps = {"source": _FEEDER_SOURCES}

    first, _ = build_scan_command(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )
    continuation, _ = build_scan_command(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
        batch_start=4,
    )

    assert not any(part.startswith("--batch-start") for part in first)
    assert "--batch-start=4" in continuation


def test_build_scan_command_auto_size_omits_geometry_without_ranges(
    tmp_path: Path,
) -> None:
    caps = {"x": Capability(kind="other")}

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert "-x" not in command
    assert "-y" not in command


def test_build_scan_command_reports_the_snapped_resolution(tmp_path: Path) -> None:
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}

    command, effective = build_scan_command(
        _config(resolution=300), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--resolution") + 1] == "150"
    assert effective.resolution == 150


def test_scan_to_files_returns_the_effective_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert result.settings.resolution == 150


def test_scan_to_files_reprobes_with_the_mapped_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF", "ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    probes: list[tuple[tuple[str, str], ...]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    # Bare, source-applied, the acquisition state the resolution is
    # assessed against (no mode option exists here), and finally the same
    # state with that resolution applied, which is what the geometry and
    # the sensor reads are taken from.
    assert probes == [
        (),
        (("--source", "ADF Duplex"),),
        (("--source", "ADF Duplex"),),
        (("--source", "ADF Duplex"), ("--resolution", "300")),
    ]


def _shrinking_window_probe(
    fail_with_resolution: bool = False, window: bool = True
) -> Callable[..., dict[str, Capability]]:
    """A backend whose window shrinks once the resolution is applied.

    Advertises 216x900 mm until the dpi is set and 200x300 mm at 600 dpi,
    which is the constraint reload SANE explicitly permits. With
    ``fail_with_resolution`` the resolution-applied listing is refused, so
    only the stale pre-resolution geometry would be left.
    """

    def probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        applied = dict(settings)
        with_resolution = applied.get("--resolution") == "600"
        if with_resolution and fail_with_resolution:
            raise DeviceError("cannot list with a resolution applied")
        caps: dict[str, Capability] = {
            "resolution": Capability(kind="enum", choices=["300", "600"])
        }
        if window:
            width, height = (200.0, 300.0) if with_resolution else (216.0, 900.0)
            caps["x"] = Capability(kind="range", minimum=0, maximum=width)
            caps["y"] = Capability(kind="range", minimum=0, maximum=height)
        return caps

    return probe


def test_geometry_is_read_with_the_negotiated_resolution_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SANE lets any option change reload every other constraint, and a
    # window that shrinks at the chosen dpi is exactly that: both axes
    # must come from the snapshot the scan's own state produces, not from
    # one taken before the resolution was applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", _shrinking_window_probe()
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    assert command[command.index("-x") + 1] == "200"
    assert command[command.index("-y") + 1] == "300"
    # The window the pipeline arms content sizing with must be the one the
    # scan actually ran in; 216x900 would make a full frame look cropped.
    assert result.settings.window_mm == (200.0, 300.0)


def test_auto_size_refuses_a_window_the_resolution_never_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Automatic page size decides whether a frame was hardware-cropped by
    # comparing it against the requested window, so a stale window is not
    # a harmless fallback: a 216x300 frame from a window recorded as
    # 216x900 reads as cropped and skips content sizing entirely. Refuse
    # before any paper moves rather than mis-size the result.
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True),
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("no paper may be fed on unverified geometry")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="scan window"):
        scan_to_files(
            _config(resolution=600, page_size="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_a_fixed_page_size_survives_a_refused_resolution_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fixed size never compares the frame against the window, so the
    # stale listing costs nothing and the tolerant path stands.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True),
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="a4"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands and result.settings.resolution == 600


def test_a_refused_resolution_listing_without_a_window_still_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No x/y evidence means no window travels either way, so the optional
    # probe failing must not invent a new reason to refuse.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True, window=False),
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands and result.settings.window_mm is None


def test_scan_to_files_sweeps_pages_scanimage_did_not_announce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("page_0002.pnm", "page_0001.pnm"):
        (tmp_path / name).write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append(p),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert seen == result.pages


def test_a_swept_page_reports_the_lost_announcement_not_a_lost_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # page_NNNN.pnm only exists once scanimage renamed it from .part, so a
    # swept file is a completed page and is delivered as one. What went
    # missing is the announcement, and that is what the warning says: it
    # must not cast doubt on the page or send the user to a result that
    # processing may still fail to produce.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )

    warnings = [record.getMessage() for record in caplog.records]
    assert any(
        "page_0001.pnm" in text and "did not announce" in text for text in warnings
    )
    assert not any("incomplete" in text for text in warnings)


def test_scan_to_files_delivers_segment_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Announced and swept frames alike carry their acquisition identity;
    # the frame index comes from the file number, so an unannounced final
    # frame still lands on the physical sheet it belongs to.
    (tmp_path / "page_0002.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )

    def fake_run(
        command: list[str], on_page: Callable[[Path], None]
    ) -> tuple[int, str]:
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    origins: list[tuple[str, PageOrigin]] = []

    scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: origins.append((p.name, o)),
    )

    assert origins == [
        ("page_0001.pnm", PageOrigin(segment=1, frame=1)),
        ("page_0002.pnm", PageOrigin(segment=1, frame=2)),
    ]


def test_scan_to_files_raises_when_nothing_was_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_scan_to_files_reports_scan_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage",
        lambda command, on_page: (1, "scanimage: sane_start failed"),
    )

    with pytest.raises(DeviceError, match="sane_start failed"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_backend_deskew_marks_the_request_as_applied() -> None:
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "swdeskew": Capability(kind="bool"),
    }

    _, with_deskew = build_scan_command(
        _config(deskew=True), "dev", caps, "out/page_%04d.pnm"
    )
    _, without = build_scan_command(
        _config(deskew=False), "dev", caps, "out/page_%04d.pnm"
    )
    _, no_option = build_scan_command(
        _config(deskew=True),
        "dev",
        {"source": Capability(kind="enum", choices=["ADF Duplex"])},
        "out/page_%04d.pnm",
    )

    assert with_deskew.deskew_applied is True
    assert without.deskew_applied is False  # the option was set to =no
    assert no_option.deskew_applied is False  # nothing there to take the job


def _fixture_caps(name: str) -> dict[str, Capability]:
    from scanmole.options import parse_capabilities

    fixtures = Path(__file__).parent.parent / "fixtures" / "scanimage-A"
    return parse_capabilities((fixtures / name).read_text())


def test_scan_refuses_without_resolution_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --resolution option at all: the effective dpi stays UNKNOWN and
    # acquisition must fail before any paper is fed.
    caps = {"source": Capability(kind="enum", choices=["ADF Duplex"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="physical resolution"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_fixed_resolution_reaches_settings_without_being_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inactive option pinned to 200 dpi establishes the effective dpi:
    # nothing is emitted, but the settings and the settings event carry it.
    import io
    import json

    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="enum", choices=["200dpi"], active=False),
    }
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert "--resolution" not in commands[0]
    assert result.settings.resolution == 200
    settings_event = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "settings"
    )
    assert settings_event["resolution"] == 200


def test_source_dependent_snapshot_decides_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The authoritative source-applied listing advertises a smaller range
    # than the bare one; the emitted and reported dpi must follow it.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    bare = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    sourced = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=150),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        return sourced if settings else bare

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def _res(minimum: float, maximum: float) -> Capability:
    return Capability(kind="range", minimum=minimum, maximum=maximum)


def test_mode_dependent_resolution_is_renegotiated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 50..600 dpi in the bare and source listings, but only 50..150 once
    # Lineart is applied: the emitted and reported dpi must come from the
    # final acquisition state, not the earlier optimistic range.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        applied_mode = any(option == "--mode" for option, _value in settings)
        return {**base, "resolution": _res(50, 150 if applied_mode else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_software_faint_resolution_follows_the_gray_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The adaptive faint path scans Gray: the dpi must come from the probe
    # with Gray (and the pinned depth) applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        gray = ("--mode", "Gray") in settings
        return {**base, "resolution": _res(50, 150 if gray else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_native_faint_resolution_follows_the_enhanced_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native SDTC path applies Lineart plus its extras; the dpi must
    # come from the probe with that complete state applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
        if ("--mode", "Lineart") in settings:
            caps["resolution"] = _res(50, 150)
        return caps

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--threshold") + 1] == "0"  # native path
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_faint_command_engages_fujitsu_sdtc_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    probes: list[tuple[tuple[str, str], ...]] = []
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return _fixture_caps("fujitsu-scansnap-ix500.txt")

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    source = ("--source", "ADF Duplex")
    final = (source, ("--mode", "Lineart"), ("--threshold", "0"), ("--variance", "0"))
    assert probes == [
        (),
        (source,),
        (source, ("--mode", "Lineart")),
        final,  # the SDTC verification reprobe
        final,  # the final-state probe deciding the resolution
        (*final, ("--resolution", "300")),  # geometry, with that dpi applied
    ]
    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 6] == [
        "--mode",
        "Lineart",
        "--threshold",
        "0",
        "--variance",
        "0",
    ]
    assert "--depth" not in command
    assert result.settings.faint_native is True


def test_faint_command_engages_epson_tet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps(
            "epson-perfection1660-epson2.txt"
        ),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    scan_to_files(
        _config(lineart_threshold="auto", source="flatbed", page_size="a4"),
        "epson2:libusb:001:004",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 4] == [
        "--mode",
        "Lineart",
        "--halftoning",
        "Text Enhanced Technology",
    ]


def test_faint_fallback_scans_gray_with_pinned_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The epsonds DS-730N has no native enhancement: the faint scan must
    # acquire Gray at an explicit 8 bit, never the device's plain Lineart.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps("epson-ds730n-epsonds.txt"),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "epsonds:net:192.168.0.167",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    assert command[command.index("--mode") + 1] == "Gray"
    assert command[command.index("--depth") + 1] == "8"
    assert result.settings.faint_native is False


def test_faint_on_a_lineart_only_device_fails_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart"]),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: caps,
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="ordinary B/W"):
        scan_to_files(
            _config(lineart_threshold="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_faint_candidate_probe_failure_falls_back_to_gray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bare and source-applied probes succeed; the candidate-mode probe
    # raises. The scan must fall back to the software path, not abort.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        if ("--mode", "Lineart") in settings:  # only the candidate probes
            raise DeviceError("timed out probing options")
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert result.settings.faint_native is False


def test_scan_to_files_warns_exactly_once_per_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Negotiation runs twice (initial probe, source-applied reprobe) but the
    # selected plan's notices must reach the user exactly once.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with caplog.at_level("INFO"):
        scan_to_files(
            _config(),  # requests adf-duplex; only a front side exists
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= 30 and "backs will not be scanned" in r.message
    ]
    assert len(warnings) == 1


def _sensor_caps() -> dict[str, Capability]:
    return {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }


def _collect_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paper: bool = True,
) -> tuple[CollectCommands, list[list[str]]]:
    """Wire a collect run: fake probes, fake sensors, owned command channel."""
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): _sensor_caps(),
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=False, page_loaded=paper),
    )
    calls: list[list[str]] = []
    return commands, calls


def test_collect_runs_segments_across_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        start = 1
        for part in cmd:
            if part.startswith("--batch-start="):
                start = int(part.split("=")[1])
        page = tmp_path / f"page_{start:04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        if len(calls) == 2:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    origins: list[PageOrigin] = []

    result = scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: origins.append(o),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert not any(part.startswith("--batch-start") for part in calls[0])
    assert "--batch-start=2" in calls[1]
    assert origins == [
        PageOrigin(segment=1, frame=1),
        PageOrigin(segment=2, frame=1),
    ]


def test_collect_emits_settings_once_and_reads_sensors_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): _sensor_caps(),
    )
    sensor_settings: list[tuple[tuple[str, str], ...]] = []

    def fake_sensors(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> SensorSnapshot:
        sensor_settings.append(tuple(settings))
        return SensorSnapshot(scan=False, page_loaded=True)

    monkeypatch.setattr("scanmole.scanner.probe_sensors", fake_sensors)
    runs: list[int] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        runs.append(1)
        page = tmp_path / f"page_{len(runs):04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        if len(runs) == 2:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert events.count("settings") == 1
    # Every sensor read applied the final acquisition settings, so the
    # snapshot is the same option-dependent state the scan will use.
    assert sensor_settings
    assert set(sensor_settings) == {
        (("--source", "ADF Front"), ("--resolution", "300"))
    }


def test_collect_counts_sheets_the_way_the_pipeline_pairs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A device that advertises no usable sources leaves the duplex request
    # standing, and that is what the pipeline pairs front and back frames
    # by. The waiting status must agree: two frames of one duplex sheet
    # are one sheet, never two.
    commands = CollectCommands()
    commands.feed_line("next\n")  # start the first segment at once
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=None, page_loaded=None),
    )

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        for index in (1, 2):  # front and back of one physical sheet
            page = tmp_path / f"page_{index:04d}.pnm"
            page.write_bytes(b"P4\n1 1\n\x00")
            on_page(page)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert result.settings.duplex is True
    waiting = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "waiting"
    ]
    assert [(event["sheets"], event["pages"]) for event in waiting] == [(1, 2)]


def test_collect_numbers_the_next_segment_past_unannounced_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        if len(calls) == 1:
            announced = tmp_path / "page_0001.pnm"
            announced.write_bytes(b"P4\n1 1\n\x00")
            on_page(announced)
            # A completed frame the announcement race missed.
            (tmp_path / "page_0002.pnm").write_bytes(b"P4\n1 1\n\x00")
        else:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    seen: list[tuple[str, PageOrigin]] = []

    scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append((p.name, o)),
    )

    # The sweep delivered the unannounced frame on its own segment and the
    # next invocation numbered past it instead of overwriting it.
    assert ("page_0002.pnm", PageOrigin(segment=1, frame=2)) in seen
    assert "--batch-start=3" in calls[1]


def test_collect_stops_when_a_swept_frame_fails_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A swept frame goes through the same callback as an announced one, so
    # a failure there must stop the collection and reach the ordinary
    # preservation path, never be skipped or replaced.
    _commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    def failing_processing(page: Path, origin: PageOrigin) -> None:
        raise ValueError("page processing failed")

    with pytest.raises(ValueError, match="processing failed"):
        scan_to_files(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            failing_processing,
        )

    assert len(calls) == 1  # no further segment was started
    assert (tmp_path / "page_0001.pnm").exists()  # left exactly as written


def test_collect_idle_timeout_without_pages_raises_no_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commands, _calls = _collect_setup(tmp_path, monkeypatch, paper=False)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)

    def never_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        raise AssertionError("scanimage must not start against an empty feeder")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_a_staging_file_is_never_swept_into_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupted scanimage leaves its in-progress raster beside the
    # completed pages as page_NNNN.pnm.part (measured on sane-backends
    # 1.4.0). It is not a page: never delivered, never counted, and never
    # the frame a later segment numbers past.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
    (tmp_path / "page_0002.pnm.part").write_bytes(b"P4\n1 1\n")  # incomplete
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append(p),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm"]
    assert seen == result.pages
    assert (tmp_path / "page_0002.pnm.part").exists()  # left exactly as found
