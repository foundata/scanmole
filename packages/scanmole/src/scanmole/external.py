"""Helpers for invoking the external tools ScanMole depends on.

All commands run as argument sequences with an explicit timeout, never through
a shell.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from scanmole.errors import MissingDependencyError

LOGGER = logging.getLogger(__name__)

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


def run_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an external command, capturing text output.

    Args:
        command: Program and arguments as a sequence.
        timeout_seconds: Maximum run time before the call raises
            ``subprocess.TimeoutExpired``.
        check: Raise ``subprocess.CalledProcessError`` on a non-zero exit.

    Returns:
        The completed process with captured ``stdout`` and ``stderr``.
    """
    LOGGER.debug("+ %s", shlex.join(command))
    # Fixed argv, never shell=True: no shell interpolation of the arguments.
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
        check=check,
    )


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
