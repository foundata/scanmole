"""GTK4/libadwaita frontend for the ScanMole CLI.

The console-script entry point lives here rather than in :mod:`scanmole.gui.app`
so a missing PyGObject/GTK installation produces a clean one-line message
instead of an import traceback: ``app`` imports the GTK bindings at module
level, while this launcher probes for them first.
"""

from __future__ import annotations

import sys

_MISSING_GUI_MESSAGE = (
    "scanmole-gui needs PyGObject and GTK 4 — install: python3-gobject gtk4 libadwaita"
)


def main(argv: list[str] | None = None) -> int:
    """Launch the GUI, or report missing GTK bindings and exit non-zero."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401  # availability probe
    except (ImportError, ValueError):
        print(f"error: {_MISSING_GUI_MESSAGE}", file=sys.stderr)
        return 1

    from scanmole.gui.app import main as run_gui

    return run_gui(argv)
