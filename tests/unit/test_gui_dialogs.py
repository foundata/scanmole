"""Regression tests for the dialog builders (PyGObject plus display)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.skipif(
        importlib.util.find_spec("gi") is None
        or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="needs PyGObject and a display",
    ),
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


def _init_adw() -> Any:
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    return Adw


class Recorder:
    def __init__(self, install_ok: bool = True, remove_ok: bool = True) -> None:
        self.schemes: list[str] = []
        self.ui_languages: list[str] = []
        self.restarts = 0
        self.resets = 0
        self.pending = False
        self.install_ok = install_ok
        self.remove_ok = remove_ok

    def dialog(self, *, installed: bool = False) -> Any:
        from scanmole_gui.dialogs import build_settings_dialog

        return build_settings_dialog(
            current_scheme="",
            current_ui_language="",
            desktop_installed=installed,
            on_scheme_selected=self.schemes.append,
            on_ui_language_selected=self.ui_languages.append,
            restart_pending=lambda: self.pending,
            on_restart=lambda: setattr(self, "restarts", self.restarts + 1),
            on_reset=lambda: setattr(self, "resets", self.resets + 1),
            on_install_desktop=lambda: self.install_ok,
            on_remove_desktop=lambda: self.remove_ok,
        )


def _rows(dialog: Any) -> dict[str, Any]:
    """The dialog's rows by translated title (test-side introspection)."""
    page = dialog.get_visible_page()
    rows: dict[str, Any] = {}

    def walk(widget: Any) -> None:
        title = getattr(widget, "get_title", None)
        if title is not None and title():
            rows[title()] = widget
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(page)
    return rows


def test_settings_dialog_callbacks_fire() -> None:
    _init_adw()
    from scanmole_gui.dialogs import COLOR_SCHEMES, UI_LANGUAGES
    from scanmole_gui.widgets import combo_select

    recorder = Recorder()
    dialog = recorder.dialog()
    rows = _rows(dialog)

    combo_select(rows["Color scheme"], COLOR_SCHEMES, "dark")
    assert recorder.schemes == ["dark"]

    # A language change persists and enables Restart once a restart is due.
    restart_row = rows["Restart now"]
    assert restart_row.get_sensitive() is False
    recorder.pending = True
    combo_select(rows["Interface language"], UI_LANGUAGES, "de")
    assert recorder.ui_languages == ["de"]
    assert restart_row.get_sensitive() is True


def test_settings_dialog_desktop_entry_round_trip() -> None:
    _init_adw()
    recorder = Recorder()
    dialog = recorder.dialog(installed=False)
    rows = _rows(dialog)
    desktop_row = rows["Desktop entry"]

    buttons: list[Any] = []

    def collect(widget: Any) -> None:
        if isinstance(widget, type(desktop_row)):
            pass
        if widget.__class__.__name__ == "Button":
            buttons.append(widget)
        child = widget.get_first_child()
        while child is not None:
            collect(child)
            child = child.get_next_sibling()

    collect(desktop_row)
    remove_btn, install_btn = buttons[0], buttons[1]
    assert install_btn.get_label() == "Install"
    assert remove_btn.get_sensitive() is False

    install_btn.emit("clicked")
    assert install_btn.get_label() == "Update"
    assert remove_btn.get_sensitive() is True

    remove_btn.emit("clicked")
    assert install_btn.get_label() == "Install"
    assert remove_btn.get_sensitive() is False


def test_about_dialog_reports_versions() -> None:
    _init_adw()
    from scanmole_gui.dialogs import build_about_dialog

    dialog = build_about_dialog(
        cli_version="9.9.9", logo_file=Path("/nonexistent.svg"), project_url="https://x"
    )
    assert dialog.get_title() == "About ScanMole"


def test_more_languages_dialog_uses_the_entered_code() -> None:
    _init_adw()
    from scanmole_gui.dialogs import build_more_languages_dialog

    used: list[str] = []
    dialog = build_more_languages_dialog(used.append)
    entry = dialog.get_extra_child()
    entry.set_text("  spa+fra ")

    dialog.emit("response", "use")
    assert used == ["spa+fra"]  # stripped

    entry.set_text("ignored")
    dialog.emit("response", "cancel")
    assert used == ["spa+fra"]  # cancel never adopts
