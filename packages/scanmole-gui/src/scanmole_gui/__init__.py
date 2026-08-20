"""GTK4/libadwaita frontend for the ScanMole CLI.

The console-script entry point lives here rather than in :mod:`scanmole_gui.app`
so a missing PyGObject/GTK installation produces a clean one-line message
instead of an import traceback: ``app`` imports the GTK bindings at module
level, while this launcher probes for them first.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import scanmole
from scanmole import BYLINE

# The scanmole-gui distribution version, bumped in lockstep with scanmole.
__version__ = "1.1.0"

_MISSING_GUI_MESSAGE = (
    "scanmole-gui needs PyGObject and GTK 4 — install: python3-gobject gtk4 libadwaita"
)


def preferred_ui_language() -> str:
    """Return the persisted GUI language override (``en``/``de``), or ``""``.

    Read without GLib on purpose: the language must be in the environment
    before :mod:`scanmole_gui.i18n` builds its gettext catalog at import time,
    which happens when the GTK probe below succeeds and ``app`` is imported.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    try:
        data = json.loads(
            (Path(config_home) / "scanmole" / "gui.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    language = data.get("ui_language") if isinstance(data, dict) else ""
    return language if language in ("en", "de") else ""


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    """Parse ``major.minor.patch`` (missing parts count as 0), or ``None``."""
    parts = text.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    numbers += [0] * (3 - len(numbers))
    return (numbers[0], numbers[1], numbers[2])


def incompatible_cli(gui_version: str, cli_version: str | None) -> str | None:
    """Return the version the GUI needs when ``cli_version`` cannot be driven.

    Returns ``None`` when the CLI is compatible. Per the contract in
    ``ARCHITECTURE.md``, compatibility is directional from 1.0.0 on: an
    older GUI may drive any newer CLI of the same SemVer major, but a newer
    GUI must refuse an older CLI (it emits options and expects behavior the
    older CLI lacks). Before 1.0.0 no promise exists, so the exact GUI
    version is required. A missing or unparsable ``cli_version`` (a CLI
    predating the ``hello`` handshake) is always incompatible.
    """
    gui = _version_tuple(gui_version)
    if gui is None or gui[0] < 1:
        return None if cli_version == gui_version else gui_version
    needed = f"{gui_version} or a newer {gui[0]}.x"
    cli = _version_tuple(cli_version or "")
    if cli is None or cli[0] != gui[0]:
        return needed
    return None if cli >= gui else needed


def main(argv: list[str] | None = None) -> int:
    """Launch the GUI, or report missing GTK bindings and exit non-zero."""
    arguments = sys.argv[1:] if argv is None else argv
    if "--version" in arguments:
        # Handled before the GTK probe so it works without PyGObject.
        print(f"scanmole-gui {__version__}\n{BYLINE}")
        return 0
    # A forced installation can pair this GUI with an older scanmole engine
    # library missing modules the GUI imports. Refuse with one clean line
    # instead of an import traceback (the packaging lower bound normally
    # prevents this pairing).
    engine_version = getattr(scanmole, "__version__", None)
    needed = incompatible_cli(__version__, engine_version)
    if needed is not None:
        print(
            f"error: scanmole-gui {__version__} needs the scanmole engine "
            f"{needed}, but version {engine_version or 'unknown'} is installed",
            file=sys.stderr,
        )
        return 1
    language = preferred_ui_language()
    if language:
        os.environ["LANGUAGE"] = language
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401  # availability probe
    except (ImportError, ValueError):
        print(f"error: {_MISSING_GUI_MESSAGE}", file=sys.stderr)
        return 1

    from scanmole_gui.app import main as run_gui

    return run_gui(argv)
