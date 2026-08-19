"""Headless supervision tests with real child processes (no GTK).

``ScanRunner`` is exercised against tiny inline Python children, because
process-group semantics (TERM/KILL escalation, descendant cleanup, pipe
EOF behavior) cannot be faked meaningfully. Synchronization never sleeps
blindly: tests wait on events with generous deadlines, timers are manual,
and every child group is torn down on the way out.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from scanmole_gui.runner import SIGKILL_GRACE_SECONDS, ScanRunner

_DEADLINE = 15.0


def _argv(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


class _Harness:
    """Collects runner callbacks; schedule is direct, timers are manual."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.exits: list[int] = []
        self.lines_at_exit = -1
        self.escalations = 0
        self.wrong_runner = False
        self.first_line = threading.Event()
        self.exited = threading.Event()
        self.timers: list[tuple[float, Callable[[], None]]] = []
        self.runner = ScanRunner(
            schedule=lambda callback: callback(),
            timer=self._timer,
            on_stdout=self._on_stdout,
            on_stderr=self._on_stderr,
            on_exit=self._on_exit,
            on_escalated=self._on_escalated,
        )

    def _timer(self, seconds: float, callback: Callable[[], None]) -> None:
        with self.lock:
            self.timers.append((seconds, callback))

    def fire_timers(self) -> None:
        with self.lock:
            pending = list(self.timers)
        for _seconds, callback in pending:
            callback()

    def _on_stdout(self, runner: ScanRunner, line: str) -> None:
        self.wrong_runner |= runner is not self.runner
        with self.lock:
            self.stdout.append(line.rstrip("\n"))
        self.first_line.set()

    def _on_stderr(self, runner: ScanRunner, line: str) -> None:
        self.wrong_runner |= runner is not self.runner
        with self.lock:
            self.stderr.append(line.rstrip("\n"))

    def _on_exit(self, runner: ScanRunner, exit_code: int) -> None:
        self.wrong_runner |= runner is not self.runner
        with self.lock:
            self.exits.append(exit_code)
            self.lines_at_exit = len(self.stdout) + len(self.stderr)
        self.exited.set()

    def _on_escalated(self, runner: ScanRunner) -> None:
        self.wrong_runner |= runner is not self.runner
        with self.lock:
            self.escalations += 1


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    instance = _Harness()
    yield instance
    if instance.runner.is_running():  # tear the whole group down, always
        instance.runner.cancel()
        instance.fire_timers()  # manual timers: force the KILL escalation
        instance.exited.wait(_DEADLINE)


def test_output_is_drained_and_exit_reported_exactly_once(
    harness: _Harness, tmp_path: Path
) -> None:
    code = (
        "import sys\n"
        "for i in range(200): print(f'line-{i}')\n"
        "for i in range(3): print(f'err-{i}', file=sys.stderr)\n"
        "sys.exit(7)\n"
    )

    harness.runner.start(_argv(code), tmp_path)

    assert harness.exited.wait(_DEADLINE)
    assert harness.exits == [7]  # exactly once
    assert harness.stdout == [f"line-{i}" for i in range(200)]  # complete, in order
    assert sorted(harness.stderr) == [f"err-{i}" for i in range(3)]
    assert harness.lines_at_exit == 203  # every line landed before the exit
    assert harness.runner.is_running() is False
    assert harness.wrong_runner is False


def test_spawn_failure_raises_and_fires_no_callbacks(
    harness: _Harness, tmp_path: Path
) -> None:
    with pytest.raises(OSError):
        harness.runner.start(["/nonexistent/scanmole-test-binary"], tmp_path)

    assert harness.exits == [] and harness.stdout == []
    assert harness.runner.poll() is None and harness.runner.is_running() is False


def test_early_pipe_close_does_not_lose_the_exit(
    harness: _Harness, tmp_path: Path
) -> None:
    code = "import os, sys\nsys.stdout.close()\nsys.stderr.close()\nos._exit(5)\n"

    harness.runner.start(_argv(code), tmp_path)

    assert harness.exited.wait(_DEADLINE)
    assert harness.exits == [5]


def test_cancel_terminates_the_whole_process_group(
    harness: _Harness, tmp_path: Path
) -> None:
    code = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    harness.runner.start(_argv(code), tmp_path)
    assert harness.first_line.wait(_DEADLINE)
    grandchild = int(harness.stdout[0])

    assert harness.runner.cancel() is True
    assert harness.exited.wait(_DEADLINE)

    assert harness.exits == [-signal.SIGTERM]
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:  # the descendant must die with the group
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"grandchild {grandchild} survived the group TERM")
    assert harness.runner.cancel() is False  # after exit: refused
    assert harness.escalations == 0  # never fired: the timers stayed manual


def test_term_to_kill_escalation(harness: _Harness, tmp_path: Path) -> None:
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    harness.runner.start(_argv(code), tmp_path)
    assert harness.first_line.wait(_DEADLINE)

    assert harness.runner.cancel() is True
    assert [seconds for seconds, _ in harness.timers] == [SIGKILL_GRACE_SECONDS]
    assert harness.runner.is_running() is True  # SIGTERM is ignored by the child

    harness.fire_timers()

    assert harness.exited.wait(_DEADLINE)
    assert harness.exits == [-signal.SIGKILL]
    assert harness.escalations == 1


def test_repeated_cancellation_is_a_no_op(harness: _Harness, tmp_path: Path) -> None:
    harness.runner.start(_argv("import time; time.sleep(300)"), tmp_path)

    first = harness.runner.cancel()
    second = harness.runner.cancel()

    assert first is True and second is False
    assert len(harness.timers) == 1  # one escalation, not one per click
    assert harness.exited.wait(_DEADLINE)


def test_cancel_after_natural_exit_is_refused(
    harness: _Harness, tmp_path: Path
) -> None:
    harness.runner.start(_argv("pass"), tmp_path)
    assert harness.exited.wait(_DEADLINE)

    assert harness.runner.cancel() is False
    assert harness.timers == []


def test_concurrent_runners_never_cross_their_streams(tmp_path: Path) -> None:
    harnesses = [_Harness() for _ in range(2)]
    try:
        for index, instance in enumerate(harnesses):
            instance.runner.start(
                _argv(f"print('marker-{index}')"), tmp_path
            )
        for instance in harnesses:
            assert instance.exited.wait(_DEADLINE)

        for index, instance in enumerate(harnesses):
            assert instance.stdout == [f"marker-{index}"]
            assert instance.wrong_runner is False
    finally:
        for instance in harnesses:
            if instance.runner.is_running():
                instance.runner.cancel()
                instance.fire_timers()
                instance.exited.wait(_DEADLINE)
