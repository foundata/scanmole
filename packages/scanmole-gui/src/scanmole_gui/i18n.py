"""Gettext catalog loading for the ScanMole GUI.

Only the GUI is localized; the CLI and its ``--json`` protocol intentionally
stay English. English is the source language: the msgids double as the
fallback whenever no catalog matches the user's locale, so English needs no
catalog of its own. The locale is taken from the usual environment variables
(``LANGUAGE``, ``LC_MESSAGES``, ``LANG``).
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "scanmole-gui"

# Compiled catalogs live inside the package (locale/<lang>/LC_MESSAGES/) and
# ship with the wheel. gettext needs a real filesystem path for localedir, so
# this resolves relative to the installed module instead of importlib.resources.
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

_translation = gettext.translation(DOMAIN, localedir=LOCALE_DIR, fallback=True)

_ = _translation.gettext
ngettext = _translation.ngettext
