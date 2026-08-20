"""Dialog construction: preferences, About and the OCR language helper.

Pure construction with explicit callbacks: the window keeps settings
storage, color-scheme application, restart/reset sequencing, desktop
installation and the dialog lifecycle; these builders only assemble
the widgets and forward the user's choices.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402  # after require_version

from scanmole_gui import __version__  # noqa: E402
from scanmole_gui.i18n import _  # noqa: E402  # after gi setup
from scanmole_gui.widgets import combo_select, combo_value  # noqa: E402

# Endonyms on purpose: a language name is most recognizable in itself.
UI_LANGUAGES = ((_("System default"), ""), ("English", "en"), ("Deutsch", "de"))
COLOR_SCHEMES = ((_("System default"), ""), (_("Light"), "light"), (_("Dark"), "dark"))


def build_settings_dialog(
    *,
    current_scheme: str,
    current_ui_language: str,
    desktop_installed: bool,
    on_scheme_selected: Callable[[str], None],
    on_ui_language_selected: Callable[[str], None],
    restart_pending: Callable[[], bool],
    on_restart: Callable[[], None],
    on_reset: Callable[[], None],
    on_install_desktop: Callable[[], bool],
    on_remove_desktop: Callable[[], bool],
) -> Adw.PreferencesDialog:
    """Build the settings dialog (color scheme, language, reset)."""
    dialog = Adw.PreferencesDialog(title=_("Settings"))
    page = Adw.PreferencesPage()
    group = Adw.PreferencesGroup()

    scheme_row = Adw.ComboRow(title=_("Color scheme"))
    scheme_row.set_model(Gtk.StringList.new([label for label, _value in COLOR_SCHEMES]))
    combo_select(scheme_row, COLOR_SCHEMES, current_scheme)

    def scheme_changed(*_a: object) -> None:
        on_scheme_selected(combo_value(scheme_row, COLOR_SCHEMES))

    scheme_row.connect("notify::selected", scheme_changed)
    group.add(scheme_row)

    lang_row = Adw.ComboRow(
        title=_("Interface language"), subtitle=_("Restart required")
    )
    lang_row.set_model(Gtk.StringList.new([label for label, _value in UI_LANGUAGES]))
    combo_select(lang_row, UI_LANGUAGES, current_ui_language)
    group.add(lang_row)

    desktop_row = Adw.ActionRow(
        title=_("Desktop entry"),
        subtitle=_("Show ScanMole in the application launcher and window switcher"),
    )
    remove_btn = Gtk.Button(label=_("Remove"), valign=Gtk.Align.CENTER)
    remove_btn.add_css_class("destructive-action")
    remove_btn.set_sensitive(desktop_installed)
    desktop_btn = Gtk.Button(valign=Gtk.Align.CENTER)
    desktop_btn.set_label(_("Update") if desktop_installed else _("Install"))

    def install_clicked(*_a: object) -> None:
        if on_install_desktop():
            desktop_btn.set_label(_("Update"))
            remove_btn.set_sensitive(True)
            dialog.add_toast(Adw.Toast(title=_("Desktop entry installed.")))
        else:
            dialog.add_toast(Adw.Toast(title=_("Could not install the desktop entry.")))

    def remove_clicked(*_a: object) -> None:
        if on_remove_desktop():
            desktop_btn.set_label(_("Install"))
            remove_btn.set_sensitive(False)
            dialog.add_toast(Adw.Toast(title=_("Desktop entry removed.")))
        else:
            dialog.add_toast(Adw.Toast(title=_("Could not remove the desktop entry.")))

    desktop_btn.connect("clicked", install_clicked)
    remove_btn.connect("clicked", remove_clicked)
    desktop_row.add_suffix(remove_btn)
    desktop_row.add_suffix(desktop_btn)
    desktop_row.set_activatable_widget(desktop_btn)
    group.add(desktop_row)

    reset_row = Adw.ActionRow(
        title=_("Reset settings"),
        subtitle=_("Restore all options to their defaults"),
    )
    reset_btn = Gtk.Button(valign=Gtk.Align.CENTER)
    reset_btn.set_child(
        Adw.ButtonContent(icon_name="edit-undo-symbolic", label=_("Reset to defaults"))
    )
    reset_btn.add_css_class("destructive-action")
    reset_btn.connect("clicked", lambda *_a: on_reset())
    reset_row.add_suffix(reset_btn)
    reset_row.set_activatable_widget(reset_btn)
    group.add(reset_row)

    # Inactive until a change actually needs a restart (the interface
    # language differs from what this process started with).
    restart_row = Adw.ActionRow(
        title=_("Restart now"),
        subtitle=_("Apply changes that require a restart"),
    )
    restart_btn = Gtk.Button(valign=Gtk.Align.CENTER)
    restart_btn.set_child(
        Adw.ButtonContent(icon_name="view-refresh-symbolic", label=_("Restart"))
    )
    restart_btn.add_css_class("suggested-action")
    restart_btn.connect("clicked", lambda *_a: on_restart())
    restart_row.add_suffix(restart_btn)
    restart_row.set_activatable_widget(restart_btn)
    group.add(restart_row)

    def update_restart_row() -> None:
        restart_row.set_sensitive(restart_pending())

    def language_changed(*_a: object) -> None:
        on_ui_language_selected(combo_value(lang_row, UI_LANGUAGES))
        update_restart_row()

    lang_row.connect("notify::selected", language_changed)
    update_restart_row()

    page.add(group)
    dialog.add(page)
    return dialog


def build_about_dialog(
    *, cli_version: str | None, logo_file: Path, project_url: str
) -> Adw.Dialog:
    """Build the flat, single-page About dialog (no nested subpages)."""
    dialog = Adw.Dialog(title=_("About ScanMole"), content_width=440)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    content = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=14,
        margin_top=12,
        margin_bottom=20,
        margin_start=20,
        margin_end=20,
    )

    identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    if logo_file.is_file():
        logo = Gtk.Image.new_from_file(str(logo_file))
        logo.set_pixel_size(72)
        identity.append(logo)
    id_labels = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2
    )
    name = Gtk.Label(label="ScanMole", xalign=0.0)
    name.add_css_class("title-4")
    id_labels.append(name)
    tagline = Gtk.Label(label=_("Easy document scanning for Linux"), xalign=0.0)
    tagline.add_css_class("dim-label")
    id_labels.append(tagline)
    identity.append(id_labels)
    content.append(identity)

    facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for key, value in (
        ("scanmole CLI", cli_version or _("unknown")),
        ("scanmole-gui", __version__),
        (_("License"), "GPL-3.0-or-later"),
    ):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        key_label = Gtk.Label(label=key, xalign=0.0, hexpand=True)
        row.append(key_label)
        value_label = Gtk.Label(label=value, xalign=1.0)
        value_label.add_css_class("monospace")
        row.append(value_label)
        facts.append(row)
    content.append(facts)

    description = Gtk.Label(
        label=_(
            "ScanMole scans documents through SANE, detects blank pages, "
            "assembles PDFs, and optionally makes them text-searchable "
            "with OCR."
        ),
        xalign=0.0,
        wrap=True,
    )
    description.add_css_class("dim-label")
    content.append(description)

    website = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    website_label = Gtk.Label(label=_("Website:"), valign=Gtk.Align.CENTER)
    website.append(website_label)
    link = Gtk.LinkButton.new_with_label(
        project_url, "foundata.com/en/projects/scanmole"
    )
    link.set_halign(Gtk.Align.START)
    website.append(link)
    content.append(website)

    toolbar.set_content(content)
    dialog.set_child(toolbar)
    return dialog


def build_more_languages_dialog(
    on_use: Callable[[str], None],
) -> Adw.AlertDialog:
    """Explain OCR language management and take a custom code to use."""
    dialog = Adw.AlertDialog(
        heading=_("More OCR Languages"),
        body=_(
            "OCR languages are Tesseract language packs, installed and "
            "removed with the distribution's package manager. The codes "
            "are three-letter ISO 639-2/T codes (deu, eng, fra, ...).\n"
            "\n"
            "Fedora: sudo dnf install tesseract-langpack-fra\n"
            "Debian/Ubuntu: sudo apt install tesseract-ocr-fra\n"
            "(remove instead of install to uninstall)\n"
            "\n"
            "To use installed languages here, enter the codes below; "
            "combine several with +."
        ),
    )
    # Roughly double the default alert width so the install commands fit
    # on one line; the extra child's minimum width backs the request up.
    dialog.set_content_width(680)
    entry = Gtk.Entry(placeholder_text="deu+fra")
    entry.set_size_request(440, -1)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("use", _("Use language"))
    dialog.set_response_appearance("use", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("use")
    dialog.set_close_response("cancel")

    def on_response(_dialog: object, response: str) -> None:
        code = entry.get_text().strip()
        if response == "use" and code:
            on_use(code)

    dialog.connect("response", on_response)
    return dialog
