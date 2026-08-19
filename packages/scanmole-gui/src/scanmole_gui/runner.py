"""Scan subprocess supervision (no GTK imports).

Owns the lifecycle of one ``scanmole --json`` run: spawning in its own
session (process group), pumping both pipes line by line, reporting the
exit exactly once, and the cancel path with its TERM-to-KILL escalation.
Every callback is dispatched through the injected ``schedule`` so the GUI
can marshal onto GLib's main loop; the escalation delay runs through the
injected ``timer``. Tests inject direct calls and manual timers, so all of
this is exercised headless.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

LOGGER = logging.getLogger(__name__)

SIGKILL_GRACE_SECONDS = 10
"""Between SIGTERM and SIGKILL on cancel.

Must outlast the engine's own worst-case cleanup: on SIGTERM the CLI gives
scanimage up to 5 seconds before killing it, then sizes and preserves the
scanned pages and removes the output reservation. Killing the group earlier
would destroy exactly the recovery the escalation is meant to allow.
"""

Schedule = Callable[[Callable[[], None]], None]
"""Dispatch a callback onto the owner's event loop (or run it directly)."""

Timer = Callable[[float, Callable[[], None]], None]
"""Run a callback once after a delay in seconds."""


class ScanRunner:
    """Supervises one scan subprocess from spawn to the exit report.

    The line and exit callbacks receive the runner itself, so the owner
    can drop stale reports after starting a newer run; ``on_escalated``
    fires when the KILL escalation was actually needed. The child gets its
    own session, so one ``killpg`` reaches every descendant.
    """

    def __init__(
        self,
        *,
        schedule: Schedule,
        timer: Timer,
        on_stdout: Callable[[ScanRunner, str], None],
        on_stderr: Callable[[ScanRunner, str], None],
        on_exit: Callable[[ScanRunner, int], None],
        on_escalated: Callable[[ScanRunner], None] | None = None,
    ) -> None:
        self._schedule = schedule
        self._timer = timer
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._on_exit = on_exit
        self._on_escalated = on_escalated
        self._proc: subprocess.Popen[str] | None = None
        self._cancelling = False

    def start(self, argv: list[str], cwd: Path) -> None:
        """Spawn the subprocess and begin supervising it.

        Raises:
            OSError: If spawning fails; no callbacks fire in that case.
        """
        self._proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group -> clean killpg
        )
        threading.Thread(target=self._supervise, daemon=True).start()

    def poll(self) -> int | None:
        """The child's exit code, or ``None`` while it is alive."""
        return self._proc.poll() if self._proc is not None else None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> bool:
        """TERM the process group now, KILL it after the grace period.

        Repeat-safe: only the first call on a live child acts and returns
        ``True``; later calls (and calls after exit) are no-ops.
        """
        if not self.is_running() or self._cancelling:
            return False
        self._cancelling = True
        self._signal_group(signal.SIGTERM)
        self._timer(SIGKILL_GRACE_SECONDS, self._escalate)
        return True

    def _escalate(self) -> None:
        if self.is_running():
            if self._on_escalated is not None:
                self._on_escalated(self)
            self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        proc = self._proc
        if proc is None:  # pragma: no cover -- cancel() guards this
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # already gone, or never ours to signal

    def _supervise(self) -> None:
        """Worker thread: pump both pipes, wait, then report exactly once."""
        proc = self._proc
        if proc is None:  # pragma: no cover -- start() sets it before the thread
            return
        pumps = [
            threading.Thread(
                target=self._pump, args=(proc.stdout, self._on_stdout), daemon=True
            ),
            threading.Thread(
                target=self._pump, args=(proc.stderr, self._on_stderr), daemon=True
            ),
        ]
        for pump in pumps:
            pump.start()
        exit_code = proc.wait()
        for pump in pumps:
            pump.join(timeout=5)  # pipes hit EOF at process exit
        for stream in (proc.stdout, proc.stderr):
            # Deterministic teardown: never leave the pipe objects to the
            # garbage collector (whose finalizer runs at an arbitrary point
            # in an arbitrary thread).
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover -- close cannot really fail
                    pass
        self._schedule(lambda: self._on_exit(self, exit_code))

    def _pump(
        self, stream: IO[str] | None, handler: Callable[[ScanRunner, str], None]
    ) -> None:
        """Forward each line from ``stream`` through the scheduler."""
        if stream is None:  # pragma: no cover -- the child is started with PIPE
            return
        try:
            for line in stream:

                def deliver(line: str = line) -> None:
                    handler(self, line)

                self._schedule(deliver)
        except (OSError, ValueError):
            pass  # the pipe went away with the process; exit reporting covers it
