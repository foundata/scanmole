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

from scanmole.errors import MissingDependencyError

LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 60
"""Timeout for quick device queries (``scanimage -f`` / ``-A``)."""

SCAN_TIMEOUT_SECONDS = 3600
"""Timeout for a full ADF batch scan."""

TOOL_TIMEOUT_SECONDS = 3600
"""Timeout for ``img2pdf`` and ``ocrmypdf``."""

INSTALL_HINT = (
    "try: sudo dnf install sane-backends img2pdf ocrmypdf tesseract-langpack-deu"
)


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
