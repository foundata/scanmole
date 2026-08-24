"""The scanimage subprocess's streaming lifecycle (no scan policy).

One function owns driving a started batch: spawning ``scanimage``,
delivering the page announcements ``--batch-print`` writes to stdout,
logging stderr progress, and the unconditional shutdown-and-drain path
with its timeout, TERM-to-KILL and cause-precedence rules. What to scan
(negotiation, command construction) and what a finished or failed batch
means (exit codes, sweeps, recovery) live in :mod:`scanmole.scanner`
and :mod:`scanmole.pipeline`; this module raises or returns.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from scanmole.errors import DeviceError, ScanMoleError, Terminated
from scanmole.external import SCAN_TIMEOUT_SECONDS

LOGGER = logging.getLogger(__name__)

__all__ = ["run_scanimage"]

REAP_GRACE_SECONDS = 5.0
"""The scanimage child's own cleanup window between TERM and KILL."""

_FAILURE_POLL_SECONDS = 0.2
"""Wait slice of the scan wait; bounds how late a callback failure or the
absolute scan deadline is noticed while the child keeps running."""

_PAGE_NAME = re.compile(r"page_\d+\.pnm")
_SCANNED_PAGE = re.compile(r"^Scanned page \d+")


def run_scanimage(
    command: list[str], on_page: Callable[[Path], None]
) -> tuple[int, str]:
    """Run a batch scan, reporting each completed page while it runs.

    ``--batch-print`` makes scanimage print each page's file name to stdout as
    soon as the page is written; ``on_page`` is called with that path from a
    reader thread, so callers can analyze pages and stream progress while the
    rest of the batch is still scanning. stderr is logged as progress.

    Lifecycle invariant: no reader thread and no delivered page callback is
    still active when this function returns or raises. Every ending (normal
    exit, timeout, callback failure, SIGINT/SIGTERM or any other exception)
    goes through one shutdown-and-drain path: the child is reaped, page
    announcements already in the pipe are delivered in order, both readers
    finish, then the pipes close. An interrupt during that drain is recorded
    and raised afterwards; it never skips the drain and never replaces an
    earlier terminating cause.

    Returns:
        The exit code and the collected stderr text.

    Raises:
        DeviceError: If the scan exceeds :data:`SCAN_TIMEOUT_SECONDS`.
        ScanMoleError: If ``on_page`` raised. A domain error propagates as-is;
            anything else is chained into a :class:`ScanMoleError`. The scan
            subprocess is terminated first, so no further pages are acquired.
    """
    LOGGER.debug("+ %s", shlex.join(command))
    lines: list[str] = []
    failure_lock = threading.Lock()
    failure_event = threading.Event()
    page_failure: Exception | None = None

    def pump_stderr(pipe: IO[str]) -> None:
        for raw in pipe:
            line = raw.rstrip("\n")
            lines.append(line)
            if _SCANNED_PAGE.match(line):
                LOGGER.info("%s ...", line.split(".")[0])
            else:
                LOGGER.debug("scanimage: %s", line)

    def pump_stdout(pipe: IO[str]) -> None:
        # A failing page callback must fail the whole batch: continuing would
        # let the run end in a misleading success, "all blank" or empty-feeder
        # result. Record the error for the controlling thread, stop the scan
        # and stop delivering pages.
        nonlocal page_failure
        for raw in pipe:
            name = raw.strip()
            if not name or not _PAGE_NAME.fullmatch(Path(name).name):
                continue
            try:
                on_page(Path(name))
            except Exception as exc:
                with failure_lock:
                    page_failure = exc
                failure_event.set()  # wakes the controller's deadline wait
                LOGGER.debug("page callback failed for %s", name, exc_info=True)
                process.terminate()
                break

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    stdout, stderr = process.stdout, process.stderr
    if stdout is None or stderr is None:  # unreachable: PIPE yields streams
        process.kill()
        process.wait()
        raise DeviceError("scanimage produced no output streams")
    # Non-daemon on purpose: their lifetime is owned here and every path
    # below joins them before returning or raising.
    stdout_reader = threading.Thread(
        target=pump_stdout, args=(stdout,), name="scanmole-stdout-reader"
    )
    stderr_reader = threading.Thread(
        target=pump_stderr, args=(stderr,), name="scanmole-stderr-reader"
    )
    cause: BaseException | None = None
    exit_code = -1
    started: list[threading.Thread] = []
    try:
        for reader in (stdout_reader, stderr_reader):
            reader.start()
            started.append(reader)

        def wait_for_scan() -> int:
            """Reap the child under one absolute deadline, watching failures.

            The reader terminates the child when a page callback fails,
            but a child ignoring TERM must not keep the controller inside
            an hour-long wait that then misreports the processing error
            as a scan timeout: the failure event ends the wait promptly
            and the shutdown path below applies the KILL escalation. A
            genuine timeout that fires first keeps precedence (the event
            is only honored while the deadline has not passed).
            """
            deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
            while not failure_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, SCAN_TIMEOUT_SECONDS)
                try:
                    return process.wait(timeout=min(_FAILURE_POLL_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
            return -1  # callback failure: reaped and reported below

        def recorded_failure() -> BaseException | None:
            """The recorded page-callback failure, wrapped for raising."""
            with failure_lock:
                failure = page_failure
            if failure is None:
                return None
            if isinstance(failure, ScanMoleError):
                return failure
            wrapped = ScanMoleError(f"page processing failed: {failure}")
            wrapped.__cause__ = failure
            return wrapped

        try:
            exit_code = wait_for_scan()
        except subprocess.TimeoutExpired as exc:
            timeout_error = DeviceError(f"scan timed out after {SCAN_TIMEOUT_SECONDS}s")
            timeout_error.__cause__ = exc
            cause = timeout_error
            process.kill()
        except BaseException as exc:  # SIGINT/SIGTERM: stop acquiring, drain
            cause = exc
            process.terminate()
        if cause is None and failure_event.is_set():
            # The callback failure ended the wait: promote it to the
            # terminating cause now, so an interrupt landing in the drain
            # below cannot replace the real diagnosis. A timeout or
            # interrupt that fired first keeps precedence (the branches
            # above already recorded it).
            cause = recorded_failure()
    except BaseException as exc:  # an interrupt outside the wait itself
        cause = exc
        process.terminate()
    finally:
        # The one unconditional shutdown-and-drain path. Whatever stopped
        # the batch (normal exit, timeout, callback failure, interrupt):
        # reap the child, let the stdout reader consume every announcement
        # already in the pipe and finish its callbacks in order, drain the
        # stderr reader, and only then close the pipes. Returning or
        # raising earlier would race the caller's recovery logic against a
        # page that is still being analyzed; a bounded join is exactly
        # that bug with extra steps. Interrupts arriving during the drain
        # are recorded (the first becomes the terminating cause when none
        # exists yet) and the drain continues.
        def absorbing(step: Callable[[], object]) -> None:
            """Run one drain step; interrupts retry it, failures never do.

            KeyboardInterrupt (SIGINT) and Terminated (the CLI's SIGTERM
            translation) are recorded and the step retried, so hammering
            Ctrl-C cannot skip the drain. Anything else is a genuine
            cleanup failure: recorded once as the terminating cause when
            none exists yet and not retried (retrying a persistent failure,
            e.g. joining a reader that never started, would loop forever);
            the remaining drain steps still run.
            """
            nonlocal cause
            while True:
                try:
                    step()
                    return
                except (KeyboardInterrupt, Terminated) as exc:
                    if cause is None:
                        # A callback that failed earlier in this drain (a
                        # buffered page) stays the diagnosis; only a truly
                        # first interrupt becomes the cause.
                        cause = recorded_failure() or exc
                except BaseException as exc:
                    if cause is None:
                        cause = exc
                    return

        def reap() -> None:
            if process.poll() is not None:
                return
            try:
                # The child's own cleanup window after the TERM.
                process.wait(timeout=REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        absorbing(reap)
        for reader in started:  # join only what actually started
            absorbing(reader.join)
        absorbing(lambda: _close_stream(stdout))
        absorbing(lambda: _close_stream(stderr))
    if cause is not None:
        raise cause
    late_failure = recorded_failure()
    if late_failure is not None:
        raise late_failure
    return exit_code, "\n".join(lines)


def _close_stream(stream: IO[str]) -> None:
    """Close one child pipe; a tiny seam so tests can inject failures."""
    stream.close()
