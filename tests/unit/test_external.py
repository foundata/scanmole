"""Tests for supervised command execution, tool lookup and install hints."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scanmole.errors import MissingDependencyError
from scanmole.external import (
    hint_for_distro,
    parse_distro_ids,
    require_tools,
    run_command,
)

_DEADLINE = 10.0


def _forking_child(pid_file: Path, *, ignore_term: bool) -> list[str]:
    """An inline child that forks a descendant and records its pid."""
    hardened = (
        "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        if ignore_term
        else ""
    )
    grand = f"{hardened}import time\ntime.sleep(300)"
    code = (
        f"{hardened}"
        "import pathlib, subprocess, sys, time\n"
        f"grand = subprocess.Popen([sys.executable, '-c', {grand!r}])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(grand.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(300)\n"
    )
    return [sys.executable, "-u", "-c", code]


def _read_pid(pid_file: Path) -> int:
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        try:
            return int(pid_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    pytest.fail("the child never recorded its descendant's pid")


def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + _DEADLINE
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)  # clean up before failing
    pytest.fail(f"descendant {pid} survived the group shutdown")


def test_timeout_terminates_obedient_descendants(tmp_path: Path) -> None:
    pid_file = tmp_path / "grand.pid"

    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_command(_forking_child(pid_file, ignore_term=False), timeout_seconds=0.5)

    _assert_dead(_read_pid(pid_file))
    assert "started" in str(info.value.output)  # partial stdout is preserved


def test_timeout_kills_descendants_that_ignore_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.external.GROUP_KILL_GRACE_SECONDS", 0.3)
    pid_file = tmp_path / "grand.pid"

    with pytest.raises(subprocess.TimeoutExpired):
        run_command(_forking_child(pid_file, ignore_term=True), timeout_seconds=0.5)

    _assert_dead(_read_pid(pid_file))


def _interrupting_pump(
    monkeypatch: pytest.MonkeyPatch, trigger: Path, count: int = 1
) -> None:
    """Arm the capture pump to raise KeyboardInterrupt ``count`` times."""
    from scanmole.external import _PipeCapture

    real_pump = _PipeCapture.pump
    state = {"remaining": count}

    def pump(self: object, deadline: float) -> None:
        if state["remaining"] > 0:
            state["remaining"] -= 1
            _read_pid(trigger)  # the descendant provably exists by now
            raise KeyboardInterrupt
        real_pump(self, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr("scanmole.external._PipeCapture.pump", pump)


def test_interrupt_terminates_the_whole_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "grand.pid"
    _interrupting_pump(monkeypatch, pid_file)

    with pytest.raises(KeyboardInterrupt):
        run_command(_forking_child(pid_file, ignore_term=False), timeout_seconds=30)

    _assert_dead(_read_pid(pid_file))


def _escaping_child(pid_file: Path, grand_code: str) -> list[str]:
    """A child whose descendant escapes into its own session, then exits."""
    code = (
        "import pathlib, subprocess, sys\n"
        f"grand = subprocess.Popen([sys.executable, '-u', '-c', {grand_code!r}],"
        " start_new_session=True)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(grand.pid))\n"
        "print('prefix-marker', flush=True)\n"
    )
    return [sys.executable, "-u", "-c", code]


def _fast_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scanmole.external.GROUP_KILL_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("scanmole.external.CLEANUP_DRAIN_SECONDS", 0.3)


def _kill_escapee(pid_file: Path) -> None:
    try:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_escaped_sleeping_descendant_cannot_stall_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The grandchild leaves the process group but inherits stdout: EOF
    # never comes. The cleanup deadline must bound the wait, and the
    # output prefix written before the timeout must be preserved.
    _fast_cleanup(monkeypatch)
    pid_file = tmp_path / "grand.pid"
    child = _escaping_child(pid_file, "import time; time.sleep(30)")
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as info:
            run_command(child, timeout_seconds=0.2)

        assert time.monotonic() - started < 3.0  # one bounded deadline
        assert "prefix-marker" in str(info.value.output)
    finally:
        _kill_escapee(pid_file)


def test_escaped_continuous_writer_cannot_stall_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A perpetually readable descriptor must not keep the drain alive
    # past the absolute deadline; later output is deliberately truncated.
    _fast_cleanup(monkeypatch)
    pid_file = tmp_path / "grand.pid"
    child = _escaping_child(pid_file, "while True: print('spam' * 20, flush=True)")
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(child, timeout_seconds=0.2)

        assert time.monotonic() - started < 3.0
    finally:
        _kill_escapee(pid_file)


def test_escaped_descendant_holding_both_pipes_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast_cleanup(monkeypatch)
    pid_file = tmp_path / "grand.pid"
    child = _escaping_child(pid_file, "import time; time.sleep(30)")
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(child, timeout_seconds=0.2)
        assert time.monotonic() - started < 3.0
    finally:
        _kill_escapee(pid_file)


def test_interrupts_during_cleanup_never_skip_kill_and_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The original timeout stays the cause even when Ctrl-C hammers the
    # cleanup: interrupts during the grace wait, the KILL and the reap are
    # absorbed, every step still runs, and the group ends up dead.
    _fast_cleanup(monkeypatch)
    pid_file = tmp_path / "grand.pid"
    real_killpg = os.killpg
    real_wait = subprocess.Popen.wait
    interrupts = {"killpg": 1, "wait": 1}

    def interrupting_killpg(pgid: int, sig: int) -> None:
        if interrupts["killpg"] > 0:
            interrupts["killpg"] -= 1
            raise KeyboardInterrupt
        real_killpg(pgid, sig)

    def interrupting_wait(
        self: subprocess.Popen[bytes], timeout: float | None = None
    ) -> int:
        if interrupts["wait"] > 0:
            interrupts["wait"] -= 1
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr("scanmole.external.os.killpg", interrupting_killpg)
    monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)

    with pytest.raises(subprocess.TimeoutExpired):  # the first cause wins
        run_command(_forking_child(pid_file, ignore_term=True), timeout_seconds=0.2)

    _assert_dead(_read_pid(pid_file))


def test_interrupted_run_still_kills_despite_more_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Original cause: an interrupt during the normal pump. A second
    # interrupt during the cleanup drain is absorbed; the group dies and
    # the original KeyboardInterrupt is what propagates.
    _fast_cleanup(monkeypatch)
    pid_file = tmp_path / "grand.pid"
    _interrupting_pump(monkeypatch, pid_file, count=2)

    with pytest.raises(KeyboardInterrupt):
        run_command(_forking_child(pid_file, ignore_term=False), timeout_seconds=30)

    _assert_dead(_read_pid(pid_file))


def test_completed_and_failed_commands_keep_the_run_contract(
    tmp_path: Path,
) -> None:
    ok = run_command(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout_seconds=10)
    assert (ok.returncode, ok.stdout, ok.stderr) == (3, "out\n", "err\n")

    with pytest.raises(subprocess.CalledProcessError) as info:
        run_command(
            ["sh", "-c", "echo out; echo err >&2; exit 3"],
            timeout_seconds=10,
            check=True,
        )
    assert info.value.returncode == 3
    assert info.value.output == "out\n" and info.value.stderr == "err\n"


_FEDORA_OS_RELEASE = 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n'
_UBUNTU_OS_RELEASE = 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n'
_DEBIAN_OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n'
_MINT_OS_RELEASE = 'ID=linuxmint\nID_LIKE="ubuntu debian"\n'


def test_parse_distro_ids_reads_id_and_id_like() -> None:
    assert parse_distro_ids(_UBUNTU_OS_RELEASE) == {"ubuntu", "debian"}
    assert parse_distro_ids(_MINT_OS_RELEASE) == {"linuxmint", "ubuntu", "debian"}
    assert parse_distro_ids("") == set()


def test_hint_names_apt_packages_on_debian_family() -> None:
    for text in (_UBUNTU_OS_RELEASE, _DEBIAN_OS_RELEASE, _MINT_OS_RELEASE):
        hint = hint_for_distro(parse_distro_ids(text))
        assert "apt install" in hint
        assert "sane-utils" in hint
        assert "tesseract-ocr-deu" in hint
        assert "tesseract-ocr-osd" in hint


def test_hint_defaults_to_fedora_packages() -> None:
    for text in (_FEDORA_OS_RELEASE, ""):
        hint = hint_for_distro(parse_distro_ids(text))
        assert "dnf install" in hint
        assert "sane-backends" in hint
        assert "tesseract-langpack-deu" in hint
        assert "tesseract-osd" in hint


def test_require_tools_accepts_present_tools() -> None:
    require_tools(["sh"])


def test_require_tools_reports_missing_tools_with_the_hint() -> None:
    with pytest.raises(MissingDependencyError, match="no-such-tool-xyz") as info:
        require_tools(["sh", "no-such-tool-xyz"])

    assert "install" in info.value.message
