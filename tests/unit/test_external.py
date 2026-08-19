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


def test_interrupt_terminates_the_whole_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "grand.pid"
    real_communicate = subprocess.Popen.communicate
    armed = {"value": True}

    def interrupting_communicate(
        self: subprocess.Popen[str],
        input: str | None = None,  # subprocess API name
        timeout: float | None = None,
    ) -> tuple[str, str]:
        if armed["value"]:
            armed["value"] = False
            _read_pid(pid_file)  # the descendant provably exists
            raise KeyboardInterrupt
        return real_communicate(self, input, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupting_communicate)

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
