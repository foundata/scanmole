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

    def dialog(self, *, installed: bool = False, borrowed: Any = ()) -> Any:
        from scanmole_gui.dialogs import build_settings_dialog

        return build_settings_dialog(
            borrowed_groups=borrowed,
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
        cli_version="9.9.9",
        logo_file=Path("/nonexistent.svg"),
        project_url="https://x",
        funding_url="https://y",
    )
    assert dialog.get_title() == "About ScanMole"


def _labelled_rows(root: Any) -> list[tuple[str, str]]:
    """Every two-label row below ``root`` whose first label reads as a key."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    children: list[Any] = []
    child = root.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()

    rows: list[tuple[str, str]] = []
    if (
        len(children) == 2
        and all(isinstance(item, Gtk.Label) for item in children)
        and children[0].get_text().endswith(":")
    ):
        rows.append((children[0].get_text(), children[1].get_label()))
    for item in children:
        rows.extend(_labelled_rows(item))
    return rows


def test_about_dialog_names_each_version_row_and_links_the_licence() -> None:
    _init_adw()
    from scanmole_gui import __version__
    from scanmole_gui.dialogs import build_about_dialog

    dialog = build_about_dialog(
        cli_version="9.9.9",
        logo_file=Path("/nonexistent.svg"),
        project_url="https://example.test/scanmole/",
        funding_url="https://coffee.test/",
    )

    # The licence names its own terms page, on the same project URL the
    # link block below it is built from.
    assert _labelled_rows(dialog.get_child()) == [
        ("scanmole (CLI) version:", "9.9.9"),
        ("scanmole-gui (GUI) version:", __version__),
        (
            "License:",
            '<a href="https://example.test/scanmole/#licensing">GPL-3.0-or-later</a>',
        ),
    ]


def test_about_dialog_escapes_the_version_the_cli_reported() -> None:
    # The row is markup so the licence can carry a link; the CLI version is
    # whatever the installed engine printed, so it must not be read as one.
    _init_adw()
    from scanmole_gui.dialogs import build_about_dialog

    dialog = build_about_dialog(
        cli_version="1.0 <b>&</b>",
        logo_file=Path("/nonexistent.svg"),
        project_url="https://example.test/scanmole/",
        funding_url="https://coffee.test/",
    )

    rows = dict(_labelled_rows(dialog.get_child()))
    assert rows["scanmole (CLI) version:"] == "1.0 &lt;b&gt;&amp;&lt;/b&gt;"


def test_about_dialog_links_to_the_project_and_its_funding() -> None:
    _init_adw()
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from scanmole_gui.dialogs import build_about_dialog

    dialog = build_about_dialog(
        cli_version="9.9.9",
        logo_file=Path("/nonexistent.svg"),
        project_url="https://example.test/scanmole/",
        funding_url="https://coffee.test/scanmole/",
    )

    grids: list[Any] = []

    def walk(widget: Any) -> None:
        if isinstance(widget, Gtk.Grid):
            grids.append(widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(dialog.get_child())
    assert len(grids) == 1
    links = grids[0]

    # Source and issues are anchors on the project page, so moving the
    # project only ever means changing the one URL it is built from. The
    # scheme is noise a reader does not need; funding names the offer
    # instead of the host, which says nothing about what the link does.
    assert [
        (links.get_child_at(0, row).get_text(), links.get_child_at(1, row).get_label())
        for row in range(4)
    ] == [
        (
            "Website:",
            '<a href="https://example.test/scanmole/">example.test/scanmole</a>',
        ),
        (
            "Source Code:",
            '<a href="https://example.test/scanmole/#source">'
            "example.test/scanmole/#source</a>",
        ),
        (
            "Report Issues:",
            '<a href="https://example.test/scanmole/#issues">'
            "example.test/scanmole/#issues</a>",
        ),
        (
            "Funding:",
            '<a href="https://coffee.test/scanmole/">Buy us a coffee</a> ♥',
        ),
    ]
    # The grid is what aligns them: every value starts one column in, at
    # the same place, however wide the label beside it happens to be.
    assert links.get_child_at(1, 4) is None  # no fifth row went unchecked
    assert all(links.get_child_at(1, row).get_xalign() == 0.0 for row in range(4))


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


def test_settings_rows_live_in_one_named_group() -> None:
    # The rows are named as a group so a later group can join them without
    # the existing ones reading as loose, untitled preferences.
    _init_adw()
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    dialog = Recorder().dialog()
    groups: list[Any] = []

    def walk(widget: Any) -> None:
        if isinstance(widget, Adw.PreferencesGroup):
            groups.append(widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(dialog.get_visible_page())

    assert [group.get_title() for group in groups] == ["Application"]
    # Every row still sits inside it.
    titles = set(_rows(dialog))
    assert {
        "Color scheme",
        "Interface language",
        "Desktop entry",
        "Reset settings",
        "Restart now",
    } <= titles


def test_borrowed_groups_lead_and_are_handed_back_on_close() -> None:
    # The scan form owns these groups and keeps using their rows after the
    # dialog is gone, so the page must let go of them: a preferences page
    # owns what it holds.
    Adw = _init_adw()

    lent = Adw.PreferencesGroup(title="Processing")
    row = Adw.SwitchRow(title="Deskew", active=True)
    lent.add(row)
    dialog = Recorder().dialog(borrowed=(lent,))

    groups: list[Any] = []

    def walk(widget: Any) -> None:
        if isinstance(widget, Adw.PreferencesGroup):
            groups.append(widget)
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(dialog.get_visible_page())
    assert [group.get_title() for group in groups] == ["Processing", "Application"]
    assert lent.get_parent() is not None

    dialog.emit("closed")

    assert lent.get_parent() is None  # back with the form
    assert row.get_title() == "Deskew"  # and still alive
    assert row.get_active() is True
