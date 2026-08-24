"""Tests for the scanimage streaming lifecycle, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage. The
acquisition orchestration built on top of it is pinned in the scanner
acquisition, negotiation and collect modules.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scanmole.errors import DeviceError, ProcessingError, ScanMoleError
from scanmole.scanstream import (
    run_scanimage,
)


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
    monkeypatch: pytest.MonkeyPatch,
    trigger: threading.Event,
    raised: threading.Event | None = None,
) -> None:
    """Deliver KeyboardInterrupt inside the first ``process.wait()`` call.

    Deterministic stand-in for a SIGINT arriving while the batch runs: the
    scan-timeout wait blocks until the callback has provably entered, then
    raises. Later ``wait()`` calls (the reap) behave normally.

    ``raised`` is set immediately before the interrupt, so a test that needs
    something to happen strictly after it can wait for that rather than hope
    for it.
    """
    real_wait = subprocess.Popen.wait
    state = {"armed": True}

    def wait(self: subprocess.Popen[str], timeout: float | None = None) -> int:
        if state["armed"]:
            state["armed"] = False
            assert trigger.wait(10)
            if raised is not None:
                raised.set()
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
    monkeypatch.setattr("scanmole.scanstream.SCAN_TIMEOUT_SECONDS", 0.2)
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
    interrupted = threading.Event()

    def failing_late(path: Path) -> None:
        entered.set()
        if path.name == "page_0002.pnm":
            # The ordering is the whole point, so it is waited for rather
            # than raced: the controller only enters the interruptible wait
            # while no failure is recorded, so a second page failing first
            # would test the opposite precedence under the same name.
            assert interrupted.wait(10)
            raise RuntimeError("late failure")

    _interrupting_wait(monkeypatch, entered, raised=interrupted)
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
    monkeypatch.setattr("scanmole.scanstream.SCAN_TIMEOUT_SECONDS", 20)
    monkeypatch.setattr("scanmole.scanstream.REAP_GRACE_SECONDS", 0.3, raising=False)
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
    monkeypatch.setattr("scanmole.scanstream.SCAN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("scanmole.scanstream.REAP_GRACE_SECONDS", 0.3, raising=False)
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

    monkeypatch.setattr("scanmole.scanstream._close_stream", failing_close)
    seen: list[Path] = []

    with pytest.raises(OSError, match="Bad file descriptor"):
        run_scanimage(["sh", "-c", f"echo '{page}'"], seen.append)

    assert seen == [page]  # the batch itself completed before cleanup failed
    assert _no_scanner_threads()


# ------------------------------------------------------- module boundary


def test_the_streaming_entry_point_stays_reachable_through_acquisition() -> None:
    # The pipeline and the acquisition tests have always monkeypatched
    # scanmole.scanner.run_scanimage. Splitting the stream into its own
    # module is not a reason for a caller that drives a scan to learn
    # where the driving is implemented.
    from scanmole import scanner, scanstream

    assert scanner.run_scanimage is scanstream.run_scanimage
    assert scanner.run_scanimage.__module__ == "scanmole.scanstream"
    assert "run_scanimage" in scanner.__all__


def test_scan_to_files_goes_through_the_patchable_facade_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Downstream code intercepts acquisition at scanmole.scanner
    # .run_scanimage; scan_to_files must resolve that module-level name,
    # not a closed-over or re-imported one.
    from scanmole.config import ScanConfig
    from scanmole.events import EventWriter
    from scanmole.options import Capability

    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P5\n1 1\n255\n0")
        assert callable(on_page)
        on_page(page)
        return 0, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    from scanmole.scanner import scan_to_files

    config = ScanConfig(
        device="test:0",
        source="adf",
        mode="gray",
        resolution=300,
        page_size="a4",
        despeckle=1,
        deskew=False,
        crop=False,
        ocr=False,
        lang="deu",
        rotate_pages=True,
        optimize=1,
        pdfa=False,
        blank_threshold=0.995,
        keep_blanks=False,
        from_images=None,
        keep_images=None,
        output=tmp_path / "out.pdf",
    )
    result = scan_to_files(
        config,
        "test:0",
        tmp_path,
        EventWriter(enabled=False, stream=None),
        lambda path, origin: None,
    )

    assert commands  # the fake intercepted the acquisition
    assert [page.name for page in result.pages] == ["page_0001.pnm"]


def test_streaming_needs_nothing_from_acquisition_policy() -> None:
    # The direction of the split, read off the import graph rather than
    # the prose: the stream drives a started command and knows nothing
    # about negotiation, capabilities, sensors, sheet flow, events or
    # configuration. Otherwise the two are separated on paper only.
    import ast

    from scanmole import scanstream

    tree = ast.parse(Path(scanstream.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    scanmole_imports = {name for name in imported if name.startswith("scanmole")}
    assert scanmole_imports == {"scanmole.errors", "scanmole.external"}


def test_acquisition_keeps_no_streaming_implementation() -> None:
    # After the move the acquisition module orchestrates scans through the
    # facade name only: no reader threads, no subprocess streaming of its
    # own. Thread use is the streaming tell.
    import ast

    from scanmole import scanner

    tree = ast.parse(Path(scanner.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "threading" not in imported
    assert "scanmole.scanstream" in imported
