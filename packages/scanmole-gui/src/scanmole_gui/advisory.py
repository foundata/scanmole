"""Cancellable supervision of advisory helper commands (no GTK).

Device discovery, the version handshake and capability probes run
external commands in worker threads. The scan runner owns the scan
child; this supervisor owns every advisory child, so acquisition can
take over the device without a concurrent probe still holding it, and
closing or restarting the application cannot orphan a probe process.

The engine's ``run_command`` supervision stays in charge of deadlines
and cleanup; this class only tracks the started processes through the
``on_spawn`` hook and kills their process groups on cancellation. Every
cancellation bumps a generation, which pending main-loop callbacks
compare against to drop results that were cancelled underneath them.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

TERM_GRACE_SECONDS = 1.0
"""Wait after the group TERM before escalating to SIGKILL."""

JOIN_SECONDS = 1.5
"""Bound on reaping killed children and joining worker threads."""


def _signal_group(pid: int, signum: int) -> None:
    """Signal a child's process group, tolerating an already-gone group."""
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):  # pragma: no cover -- racy
        pass


def _wait_until(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Reap the direct child if it exits before the deadline."""
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


class AdvisoryCommands:
    """Tracks advisory worker threads and their child processes.

    ``adopt`` is handed to the engine as the ``on_spawn`` hook and runs
    on the worker thread; everything else runs on the main thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._threads: list[threading.Thread] = []
        self._closed = False
        self.generation = 0
        """Bumped by every cancellation; callbacks carrying an older
        value report work that was cancelled and must drop silently."""

    def spawn_worker(self, target: Callable[..., None], *args: object) -> None:
        """Start and track a daemon worker thread running ``target``."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def adopter(self, generation: int) -> Callable[[subprocess.Popen[bytes]], None]:
        """The ``on_spawn`` hook for a worker started at ``generation``."""

        def adopt(process: subprocess.Popen[bytes]) -> None:
            self.adopt(process, generation)

        return adopt

    def adopt(self, process: subprocess.Popen[bytes], generation: int) -> None:
        """Track a started advisory child (the ``on_spawn`` hook).

        A spawn carrying a stale generation is killed immediately
        instead of being tracked: a worker resuming past a cancellation
        (a scan takeover killed its previous command, or the window is
        closing) must not leak a fresh child behind the snapshot the
        cancellation acted on.
        """
        with self._lock:
            stale = self._closed or generation != self.generation
            if not stale:
                self._prune()
                self._processes[process.pid] = process
        if stale:
            _signal_group(process.pid, signal.SIGKILL)

    def _prune(self) -> None:
        self._processes = {
            pid: process
            for pid, process in self._processes.items()
            if process.poll() is None
        }

    def cancel_pending(self, *, close: bool = False) -> bool:
        """Stop every advisory child and join the workers, boundedly.

        TERM to each child's group, a short grace, KILL for survivors,
        then a bounded reap and thread join. ``close`` additionally
        refuses future children (application shutdown or window close).

        Returns:
            Whether everything is idle afterwards. ``False`` means a
            worker thread is still wedged; its child group is dead
            either way, so no process outlives the caller.
        """
        with self._lock:
            if close:
                self._closed = True
            self.generation += 1
            self._prune()
            processes = list(self._processes.values())
            threads = [t for t in self._threads if t.is_alive()]
        for process in processes:
            _signal_group(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE_SECONDS
        for process in processes:
            _wait_until(process, deadline)
        for process in processes:
            if process.poll() is None:
                _signal_group(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + JOIN_SECONDS
        for process in processes:
            _wait_until(process, deadline)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._prune()
            idle = not self._processes and not any(t.is_alive() for t in self._threads)
        if not idle:  # pragma: no cover -- needs a wedged worker thread
            LOGGER.debug("advisory worker still busy after cancellation")
        return idle
