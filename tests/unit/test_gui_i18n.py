"""Tests for the GUI's gettext catalogs.

Imports only :mod:`scanmole.gui.i18n` (stdlib gettext), so no GTK/PyGObject is
needed. Verifies that the committed German .mo is loadable and that unknown
locales fall back to the English msgids.
"""

from __future__ import annotations

import gettext

from scanmole.gui.i18n import DOMAIN, LOCALE_DIR


def _catalog(language: str) -> gettext.NullTranslations:
    return gettext.translation(
        DOMAIN, localedir=LOCALE_DIR, languages=[language], fallback=True
    )


def test_german_catalog_translates_known_strings() -> None:
    de = _catalog("de")

    assert de.gettext("Scan") == "Scannen"
    assert de.gettext("Cancel") == "Abbrechen"
    assert de.gettext("No scanners found.") == "Keine Scanner gefunden."


def test_german_catalog_handles_plurals() -> None:
    de = _catalog("de")

    singular = de.ngettext(" (%d blank skipped)", " (%d blanks skipped)", 1)
    plural = de.ngettext(" (%d blank skipped)", " (%d blanks skipped)", 2)

    assert singular % 1 == " (1 leere Seite übersprungen)"
    assert plural % 2 == " (2 leere Seiten übersprungen)"


def test_unknown_locale_falls_back_to_english_msgids() -> None:
    fallback = _catalog("tlh")  # no Klingon catalog

    assert fallback.gettext("Scan") == "Scan"
    assert fallback.ngettext("Found %d scanner.", "Found %d scanners.", 2) == (
        "Found %d scanners."
    )
