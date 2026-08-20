"""Helpers for invoking the external tools ScanMole depends on.

All commands run as argument sequences with an explicit timeout, never through
a shell.
"""

from __future__ import annotations

import logging
import os
import select
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from scanmole.errors import MissingDependencyError, Terminated

LOGGER = logging.getLogger(__name__)

GROUP_KILL_GRACE_SECONDS = 3.0
"""Between the group TERM and the group KILL of a stopped command.

External tools spawn their own helpers (ocrmypdf drives tesseract and
Ghostscript), so a timed-out or interrupted command is signalled as a
whole process group, and the direct child's exit alone does not prove the
group is gone. Together with :data:`CLEANUP_DRAIN_SECONDS` this forms one
absolute cleanup deadline, sitting deliberately far inside the GUI
runner's ten-second outer grace: the GUI TERMs the CLI's process group,
the CLI unwinds and stops any private child group within this window,
and the GUI's later KILL remains the hard limit.
"""

CLEANUP_DRAIN_SECONDS = 2.0
"""Budget after the group KILL for the reap and the final pipe drain.

A descendant that started its own session escapes the group KILL and can
hold a duplicated pipe end open indefinitely; once this deadline passes,
the local descriptors are closed and the captured output prefix is
returned as-is. Diagnostics written after the deadline are truncated on
purpose: an escaped process must never stall the caller.
"""

PROBE_TIMEOUT_SECONDS = 60
"""Timeout for quick device queries (``scanimage -f`` / ``-A``)."""

SCAN_TIMEOUT_SECONDS = 3600
"""Timeout for a full ADF batch scan."""

TOOL_TIMEOUT_SECONDS = 3600
"""Timeout for ``img2pdf`` and ``ocrmypdf``."""

_DNF_HINT = "try: sudo dnf install sane-backends img2pdf ocrmypdf tesseract-langpack-deu tesseract-osd"
_APT_HINT = "try: sudo apt install sane-utils img2pdf ocrmypdf tesseract-ocr-deu tesseract-ocr-osd"


def parse_distro_ids(os_release: str) -> set[str]:
    """Extract the ``ID`` and ``ID_LIKE`` identifiers from os-release text."""
    ids: set[str] = set()
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids.update(value.strip().strip('"').lower().split())
    return ids


def hint_for_distro(ids: set[str]) -> str:
    """Return the package-install hint for a distro id set (Fedora default)."""
    if ids & {"debian", "ubuntu"}:
        return _APT_HINT
    return _DNF_HINT


def _detect_install_hint() -> str:
    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return _DNF_HINT
    return hint_for_distro(parse_distro_ids(os_release))


INSTALL_HINT = _detect_install_hint()


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone, or never ours to signal


class _PipeCapture:
    """Binary capture of a child's pipes with absolute-deadline pumping.

    Reads raw descriptors through ``select`` so every wait is bounded by
    an absolute deadline: EOF may simply never come when a descendant of
    the child keeps a duplicated pipe end open.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._streams = [
            stream for stream in (process.stdout, process.stderr) if stream is not None
        ]
        self._order = [stream.fileno() for stream in self._streams]
        self._buffers = {fd: bytearray() for fd in self._order}
        self._open = set(self._order)

    @property
    def drained(self) -> bool:
        return not self._open

    def pump(self, deadline: float) -> None:
        """Read until every pipe hit EOF or the deadline passed."""
        while self._open:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                ready, _, _ = select.select(list(self._open), [], [], remaining)
            except OSError:  # pragma: no cover -- fd closed underneath
                return
            if not ready:
                return  # the deadline passed
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    self._buffers[fd] += chunk
                else:
                    self._open.discard(fd)

    def close(self) -> None:
        for stream in self._streams:
            try:
                stream.close()
            except OSError:  # pragma: no cover -- close cannot really fail
                pass

    def decoded(self) -> tuple[str, str]:
        texts = [
            bytes(self._buffers[fd]).decode("utf-8", "replace") for fd in self._order
        ]
        return texts[0], texts[1]


def _wait_until(process: subprocess.Popen[bytes], deadline: float) -> None:
    """Reap the direct child if it exits before the deadline."""
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


def _cleanup(
    process: subprocess.Popen[bytes],
    capture: _PipeCapture,
    outcome: BaseException,
) -> BaseException:
    """Stop the whole group and drain, preserving the first cause.

    One absolute deadline spans TERM, the grace wait, the group KILL (sent
    even when the direct child already exited: descendants share the
    group), the reap and the final drain. Interrupts during any step are
    absorbed without restarting the deadline; unexpected step failures are
    logged at debug level and the remaining steps still run. The caller
    always sees the original cause.
    """
    kill_at = time.monotonic() + GROUP_KILL_GRACE_SECONDS
    end_at = kill_at + CLEANUP_DRAIN_SECONDS

    def absorbing(callback: Callable[[], object]) -> None:
        while True:
            try:
                callback()
                return
            except (KeyboardInterrupt, Terminated):
                continue  # deadline-bounded steps: a retry cannot extend cleanup
            except BaseException:
                LOGGER.debug("cleanup step failed; continuing", exc_info=True)
                return

    absorbing(lambda: _signal_group(process.pid, signal.SIGTERM))
    absorbing(lambda: capture.pump(kill_at))
    absorbing(lambda: _wait_until(process, kill_at))
    absorbing(lambda: _signal_group(process.pid, signal.SIGKILL))
    absorbing(lambda: _wait_until(process, end_at))
    absorbing(lambda: capture.pump(end_at))
    return outcome


def run_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    check: bool = False,
    on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command in its own process group, capturing text.

    Timeouts and interruptions stop the *whole* group (TERM, then KILL,
    then a bounded reap and drain under one absolute deadline), so
    descendants of the tools (tesseract and Ghostscript under ocrmypdf)
    cannot survive their parent, and even a descendant that escaped into
    its own session cannot stall the caller by holding a pipe open. The
    first cause always wins: a timeout stays ``TimeoutExpired`` and an
    interrupt stays itself, no matter what happens during cleanup.

    Args:
        command: Program and arguments as a sequence.
        timeout_seconds: Maximum run time before the call raises
            ``subprocess.TimeoutExpired`` (with the partial output
            attached, as ``subprocess.run`` would).
        check: Raise ``subprocess.CalledProcessError`` on a non-zero exit.
        on_spawn: Observation hook receiving the started process, so a
            caller can cancel the group from outside (a GUI stopping its
            advisory probes). It runs on the calling thread and must not
            raise; the supervision here stays in charge either way.

    Returns:
        The completed process with captured ``stdout`` and ``stderr``.
    """
    LOGGER.debug("+ %s", shlex.join(command))
    # Fixed argv, never shell=True: no shell interpolation of the arguments.
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own group: cleanup reaches descendants
    )
    if on_spawn is not None:
        on_spawn(process)
    capture = _PipeCapture(process)
    outcome: BaseException | None = None
    try:
        deadline = time.monotonic() + timeout_seconds
        capture.pump(deadline)
        if capture.drained:
            _wait_until(process, deadline)
        if process.poll() is None or not capture.drained:
            # Same contract as subprocess.run: a pipe still open past the
            # deadline is a timeout even when the direct child exited (a
            # descendant may hold a duplicated end).
            outcome = subprocess.TimeoutExpired(list(command), timeout_seconds)
    except BaseException as exc:
        outcome = exc
    if outcome is not None:
        outcome = _cleanup(process, capture, outcome)
    capture.close()
    stdout, stderr = capture.decoded()
    if isinstance(outcome, subprocess.TimeoutExpired):
        raise subprocess.TimeoutExpired(
            list(command), timeout_seconds, output=stdout, stderr=stderr
        ) from None
    if outcome is not None:
        raise outcome
    result = subprocess.CompletedProcess(
        list(command), process.returncode, stdout, stderr
    )
    if check and process.returncode != 0:
        raise subprocess.CalledProcessError(
            process.returncode, list(command), output=stdout, stderr=stderr
        )
    return result


def require_tools(tools: Sequence[str]) -> None:
    """Ensure each tool is on ``PATH``.

    Raises:
        MissingDependencyError: If one or more tools are missing.
    """
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise MissingDependencyError(
            f"missing required tool(s): {', '.join(missing)} -- {INSTALL_HINT}"
        )
