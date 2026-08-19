"""Helpers for invoking the external tools ScanMole depends on.

All commands run as argument sequences with an explicit timeout, never through
a shell.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path

from scanmole.errors import MissingDependencyError

LOGGER = logging.getLogger(__name__)

GROUP_KILL_GRACE_SECONDS = 3.0
"""Between the group TERM and the group KILL of a stopped command.

External tools spawn their own helpers (ocrmypdf drives tesseract and
Ghostscript), so a timed-out or interrupted command is signalled as a
whole process group, and the direct child's exit alone does not prove the
group is gone. The grace sits deliberately far inside the GUI runner's
ten-second outer grace: the GUI TERMs the CLI's process group, the CLI
unwinds and stops any private child group within this window, and the
GUI's later KILL remains the hard limit.
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


def _shutdown_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """TERM the command's whole group, KILL it after the grace, reap, drain.

    The group KILL is sent even when the direct child already exited: its
    descendants live in the same group and may have ignored the TERM.

    Returns:
        Whatever stdout and stderr the command produced before it died.
    """
    _signal_group(process.pid, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=GROUP_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    else:
        _signal_group(process.pid, signal.SIGKILL)
    return stdout or "", stderr or ""


def run_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an external command in its own process group, capturing text.

    Timeouts and interruptions stop the *whole* group (TERM, then KILL
    after :data:`GROUP_KILL_GRACE_SECONDS`), so descendants of the tools
    (tesseract and Ghostscript under ocrmypdf) cannot survive their
    parent. The direct child is always reaped and its pipes drained.

    Args:
        command: Program and arguments as a sequence.
        timeout_seconds: Maximum run time before the call raises
            ``subprocess.TimeoutExpired`` (with the partial output
            attached, as ``subprocess.run`` would).
        check: Raise ``subprocess.CalledProcessError`` on a non-zero exit.

    Returns:
        The completed process with captured ``stdout`` and ``stderr``.
    """
    LOGGER.debug("+ %s", shlex.join(command))
    # Fixed argv, never shell=True: no shell interpolation of the arguments.
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,  # own group: cleanup reaches descendants
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _shutdown_group(process)
        raise subprocess.TimeoutExpired(
            exc.cmd, timeout_seconds, output=stdout, stderr=stderr
        ) from None
    except BaseException:
        _shutdown_group(process)
        raise
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
