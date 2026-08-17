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

from scanmole import BYLINE

# The scanmole-gui distribution version, bumped in lockstep with scanmole.
__version__ = "1.0.0"

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


def incompatible_cli(gui_version: str, cli_version: str | None) -> str | None:
    """Return the version the GUI needs when ``cli_version`` cannot be driven.

    Returns ``None`` when the CLI is compatible. Per the contract in
    ``ARCHITECTURE.md``, GUI and CLI are compatible from 1.0.0 on if and only
    if their SemVer majors match; before 1.0.0 no such promise exists, so the
    exact GUI version is required. A missing or unparsable ``cli_version``
    (a CLI predating the ``hello`` handshake) is always incompatible.
    """
    try:
        gui_major = int(gui_version.split(".")[0])
    except ValueError:
        gui_major = 0
    if gui_major < 1:
        return None if cli_version == gui_version else gui_version
    needed = f"{gui_major}.x"
    try:
        cli_major = int((cli_version or "").split(".")[0])
    except ValueError:
        return needed
    return None if cli_major == gui_major else needed


def main(argv: list[str] | None = None) -> int:
    """Launch the GUI, or report missing GTK bindings and exit non-zero."""
    arguments = sys.argv[1:] if argv is None else argv
    if "--version" in arguments:
        # Handled before the GTK probe so it works without PyGObject.
        print(f"scanmole-gui {__version__}\n{BYLINE}")
        return 0
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
