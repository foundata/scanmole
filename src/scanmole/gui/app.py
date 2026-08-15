"""ScanMole: GTK4/libadwaita frontend for the ``scanmole`` CLI.

A deliberately thin GUI: it builds a ``scanmole --json`` command line from the
form, streams the CLI's JSON-lines events into a persistent result bar and
offers the finished PDF. All scanning and OCR work happens in the ``scanmole``
executable (resolved from ``PATH``).

Widget labels, status texts and dialogs are translatable via gettext (see
:mod:`scanmole.gui.i18n`); the log pane stays English on purpose, because it
mixes in output of the English-only CLI.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import (  # noqa: E402  # after require_version
    Adw,
    Gdk,
    Gio,
    GLib,
    Gtk,
)

from scanmole import __version__  # noqa: E402
from scanmole.gui.i18n import _, ngettext  # noqa: E402  # after gi setup

# The GUI holds no pipeline logic; this pure helper is imported only so the
# live filename preview matches what the CLI will produce.
from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE, expand_template  # noqa: E402

APP_ID = "com.foundata.ScanMole"
PROJECT_URL = "https://foundata.com/en/projects/scanmole/"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "scanmole" / "gui.json"
ICON_DIR = Path(__file__).resolve().parent / "icons"
LOGO_FILE = ICON_DIR / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"

SOURCES = (
    (_("Flatbed"), "flatbed"),
    (_("ADF"), "adf"),
    (_("ADF Duplex"), "adf-duplex"),
    (_("ADF Back"), "adf-back"),
)
MODES = ((_("B/W"), "lineart"), (_("Gray"), "gray"), (_("Color"), "color"))
RESOLUTION_PRESETS = (150, 200, 300, 600)
RESOLUTION_MINIMUM = 50
RESOLUTION_MAXIMUM = 1200
PAGE_SIZES = (
    (_("Automatic"), "auto"),
    ("A4", "a4"),
    ("A5", "a5"),
    ("A6", "a6"),
    (_("Letter"), "letter"),
    (_("Legal"), "legal"),
)
LANGUAGES = (
    (_("German (deu)"), "deu"),
    (_("English (eng)"), "eng"),
    (_("German + English (deu+eng)"), "deu+eng"),
)
# Endonyms on purpose: a language name is most recognizable in itself.
UI_LANGUAGES = ((_("System default"), ""), ("English", "en"), ("Deutsch", "de"))

# Rough size per page at 300 dpi, from measured fleet scans; scaled by dpi².
# Content-dependent, so only ever presented as an approximation.
_SIZE_BASE_MB = {"lineart": 0.1, "gray": 0.3, "color": 0.5}

# Friendly texts for the CLI's documented exit codes.
EXIT_HINTS: dict[int, tuple[str, str]] = {
    2: (
        _("No Pages Scanned"),
        _(
            "No pages were scanned — is the ADF loaded?\n"
            "(All pages may also have been detected as blank.)"
        ),
    ),
    3: (
        _("Scanner Error"),
        _(
            "The scanner reported an error. Make sure it is connected, powered "
            "on and not in use by another application, then try again."
        ),
    ),
    4: (
        _("Missing Dependency"),
        _(
            "scanmole is missing a required tool (e.g. scanimage, img2pdf, "
            "ocrmypdf) on this system. See the log for details."
        ),
    ),
    5: (
        _("Processing Failed"),
        _(
            "PDF assembly or OCR failed after scanning. The scanned pages "
            "were kept; see the log for the folder path."
        ),
    ),
}

SIGKILL_GRACE_SECONDS = 3  # between SIGTERM and SIGKILL on cancel


def find_scanmole() -> str:
    """Return the ``scanmole`` executable on ``PATH``, or the bare name."""
    return shutil.which("scanmole") or "scanmole"


def default_folder() -> str:
    """Return the XDG Documents folder, falling back to the home directory."""
    docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    return str(docs) if docs else str(Path.home())


def abbreviate_home(path: str) -> str:
    """Render a path with the home directory abbreviated to ``~``."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def load_settings() -> dict[str, object]:
    """Load persisted GUI settings, returning an empty dict on any error."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def store_settings(data: dict[str, object]) -> None:
    """Persist GUI settings; failures are ignored as a convenience feature."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def combo_value(row: Adw.ComboRow, items: tuple[tuple[str, str], ...]) -> str:
    """Return the CLI value for a combo row's selected ``(label, value)`` item."""
    index = int(row.get_selected())  # untyped GTK call
    return items[index][1] if 0 <= index < len(items) else items[0][1]


def combo_select(
    row: Adw.ComboRow, items: tuple[tuple[str, str], ...], value: str
) -> None:
    """Select the combo row item whose CLI value equals ``value``."""
    for index, (_label, item_value) in enumerate(items):
        if item_value == value:
            row.set_selected(index)
            return


def as_int(value: object, fallback: int) -> int:
    """Return ``value`` as an int when it is one, else ``fallback``.

    JSON event fields are untrusted input; plural forms need real ints.
    """
    return value if isinstance(value, int) else fallback


class ChoiceRow:
    """A preferences row choosing one of a few fixed ``(label, value)`` items.

    Renders the options as an inline ``Adw.ToggleGroup`` (all choices visible,
    per the design) when libadwaita provides it (>= 1.7); older platforms
    (e.g. Ubuntu 24.04) fall back to a plain ``Adw.ComboRow`` so the full
    option set stays available everywhere.
    """

    def __init__(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        items: tuple[tuple[str, str], ...],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Build the row inside ``group``."""
        self._items = items
        self._on_change = on_change
        if hasattr(Adw, "ToggleGroup"):
            self.row: Adw.ActionRow = Adw.ActionRow(title=title)
            self._toggles = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
            for label, _value in items:
                self._toggles.add(Adw.Toggle(label=label))
            self._toggles.connect("notify::active", self._changed)
            self.row.add_suffix(self._toggles)
            self._combo: Adw.ComboRow | None = None
        else:
            self._toggles = None
            self._combo = Adw.ComboRow(title=title)
            self._combo.set_model(Gtk.StringList.new([label for label, _v in items]))
            self._combo.connect("notify::selected", self._changed)
            self.row = self._combo
        group.add(self.row)

    def _changed(self, *_args: object) -> None:
        if self._on_change is not None:
            self._on_change()

    def value(self) -> str:
        """Return the CLI value of the selected item."""
        if self._toggles is not None:
            index = int(self._toggles.get_active())  # untyped GTK call
        else:
            assert self._combo is not None
            index = int(self._combo.get_selected())  # untyped GTK call
        return (
            self._items[index][1]
            if 0 <= index < len(self._items)
            else (self._items[0][1])
        )

    def select(self, value: str) -> None:
        """Select the item whose CLI value equals ``value`` (no-op if absent)."""
        for index, (_label, item_value) in enumerate(self._items):
            if item_value == value:
                if self._toggles is not None:
                    self._toggles.set_active(index)
                else:
                    assert self._combo is not None
                    self._combo.set_selected(index)
                return


# PyGObject has no stubs, so the GTK base class is Any; subclassing it is the
# GTK boundary that cannot be typed.
class MainWindow(Adw.ApplicationWindow):  # type: ignore[misc]
    """The single application window: scan form, log and result bar."""

    def __init__(self, **kwargs: object) -> None:
        """Build the UI, restore settings and start device discovery."""
        super().__init__(**kwargs)
        self.set_title("ScanMole")
        self.set_default_size(720, 900)

        self._scanmole = find_scanmole()
        self._settings = load_settings()

        # Runtime state
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._devices: list[dict[str, str]] = []
        self._pages = 0
        self._blanks = 0
        self._result: dict[str, object] = {}
        self._error_message: str | None = None
        self._run_folder = Path(default_folder())
        self._last_output: Path | None = None
        self._folder = str(self._settings.get("folder") or default_folder())
        self._languages: list[tuple[str, str]] = list(LANGUAGES)
        self._cli_version: str | None = None
        self._startup_ui_language = str(self._settings.get("ui_language") or "")
        self._res_syncing = False

        self._build_ui()
        self._apply_saved_settings()
        self.connect("close-request", self._on_close_request)

        self._refresh_devices()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        """Assemble the header bar, the form sections and the result bar."""
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if LOGO_FILE.is_file():
            logo = Gtk.Image.new_from_file(str(LOGO_FILE))
            logo.set_pixel_size(24)
            title_box.append(logo)
        title_label = Gtk.Label(label="ScanMole")
        title_label.add_css_class("heading")
        title_box.append(title_label)
        header.set_title_widget(title_box)
        about_btn = Gtk.Button(
            icon_name="help-about-symbolic", tooltip_text=_("About ScanMole")
        )
        about_btn.add_css_class("flat")
        about_btn.connect("clicked", self._on_about_clicked)
        header.pack_end(about_btn)
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        toolbar.set_content(scroller)

        self._clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=900)
        scroller.set_child(self._clamp)

        columns = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=24,
            margin_top=18,
            margin_bottom=18,
            margin_start=16,
            margin_end=16,
        )
        self._left_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, hexpand=True
        )
        self._right_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, hexpand=True
        )
        columns.append(self._left_column)
        columns.append(self._right_column)
        self._columns = columns
        self._clamp.set_child(columns)

        self._build_scanner_group()
        self._build_output_group()
        self._build_document_group()
        self._build_processing_group()
        self._build_application_group()
        self._build_log_area()

        self._form_groups = (
            self._scanner_grp,
            self._doc_grp,
            self._proc_grp,
            self._out_grp,
            self._app_grp,
        )

        toolbar.add_bottom_bar(self._build_result_bar())

        # Responsive layout: two columns by default (720 px wide window), one
        # column below 700 px via a breakpoint.
        self._apply_layout(wide=True)
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 700sp")
        )
        breakpoint.connect("apply", lambda *_a: self._apply_layout(wide=False))
        breakpoint.connect("unapply", lambda *_a: self._apply_layout(wide=True))
        self.add_breakpoint(breakpoint)

        self._on_document_changed()

    def _apply_layout(self, *, wide: bool) -> None:
        """Arrange the form sections in one or two columns."""
        sections_wide_left = (self._scanner_grp, self._doc_grp, self._proc_grp)
        sections_wide_right = (self._out_grp, self._app_grp, self._log_area)
        sections_narrow = (
            self._scanner_grp,
            self._out_grp,
            self._doc_grp,
            self._proc_grp,
            self._app_grp,
            self._log_area,
        )
        for section in sections_narrow:
            parent = section.get_parent()
            if parent is not None:
                parent.remove(section)
        if wide:
            for section in sections_wide_left:
                self._left_column.append(section)
            for section in sections_wide_right:
                self._right_column.append(section)
        else:
            for section in sections_narrow:
                self._left_column.append(section)
        self._right_column.set_visible(wide)
        self._columns.set_homogeneous(wide)
        self._clamp.set_maximum_size(1080 if wide else 640)
        self._clamp.set_tightening_threshold(900 if wide else 480)

    def _build_scanner_group(self) -> None:
        """Build the Scanner group including the primary Scan action."""
        self._scanner_grp = Adw.PreferencesGroup(title=_("Scanner"))
        # Never make this row insensitive: the refresh button is one of its
        # suffix children, and a disabled row would take the only way to
        # recover from an empty device list down with it.
        self._device_row = Adw.ComboRow(
            title=_("Device"),
            subtitle=_("Searching for scanners…"),
        )
        self._device_row.connect("notify::selected", self._on_device_selected)
        # The rescan action sits beside the device list, not in the header:
        # the action lives where its object is (mockup rule).
        self._refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Refresh devices"),
        )
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", self._refresh_devices)
        self._device_row.add_suffix(self._refresh_btn)
        self._scanner_grp.add(self._device_row)
        self._source_row = ChoiceRow(self._scanner_grp, _("Source"), SOURCES)

        # One primary action: Scan is the only accented control, full width at
        # the bottom of the Scanner group (mockup rule); Cancel swaps in while
        # a scan runs.
        self._scan_btn = Gtk.Button(margin_top=6)
        self._scan_btn.set_child(
            Adw.ButtonContent(
                icon_name="media-playback-start-symbolic", label=_("Scan")
            )
        )
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.add_css_class("pill")
        self._scan_btn.connect("clicked", self._on_scan_clicked)
        self._scanner_grp.add(self._scan_btn)
        self._cancel_btn = Gtk.Button(label=_("Cancel"), visible=False, margin_top=6)
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.add_css_class("pill")
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        self._scanner_grp.add(self._cancel_btn)

    def _build_output_group(self) -> None:
        """Build the Output group (folder, filename template)."""
        self._out_grp = Adw.PreferencesGroup(title=_("Output"))
        self._folder_row = Adw.ActionRow(title=_("Folder"))
        self._folder_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )
        self._folder_btn.connect("clicked", self._on_pick_folder)
        self._folder_row.add_suffix(self._folder_btn)
        self._folder_row.set_activatable_widget(self._folder_btn)
        self._out_grp.add(self._folder_row)

        self._name_row = Adw.ActionRow(title=_("File name"))
        name_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
            valign=Gtk.Align.CENTER,
            hexpand=True,
            margin_top=6,
            margin_bottom=6,
        )
        self._name_entry = Gtk.Entry(
            placeholder_text=DEFAULT_OUTPUT_TEMPLATE, width_chars=22, hexpand=True
        )
        self._name_entry.connect("changed", self._update_name_preview)
        name_box.append(self._name_entry)
        hint = Gtk.Label(
            label=_(
                "placeholders: {YYYY} {MM} {DD} {hh} {mm} {ss} · "
                "{NN} (auto-number) · {preset} {device}"
            ),
            xalign=1.0,
            wrap=True,
            justify=Gtk.Justification.RIGHT,
            max_width_chars=34,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        name_box.append(hint)
        self._name_row.add_suffix(name_box)
        self._out_grp.add(self._name_row)

    def _build_document_group(self) -> None:
        """Build the Document group (color mode, resolution, page size)."""
        self._doc_grp = Adw.PreferencesGroup(title=_("Document"))
        self._mode_row = ChoiceRow(
            self._doc_grp, _("Color mode"), MODES, self._on_document_changed
        )

        # Hybrid resolution control: a numeric dpi entry plus preset chips.
        # The static bounds are a sanity clamp only; the CLI snaps the value
        # to what the device actually supports during capability negotiation.
        self._res_row = Adw.ActionRow(title=_("Resolution"))
        res_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
            margin_top=6,
            margin_bottom=6,
        )
        spin_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END
        )
        adjustment = Gtk.Adjustment(
            lower=RESOLUTION_MINIMUM,
            upper=RESOLUTION_MAXIMUM,
            step_increment=10,
            page_increment=50,
            value=200,
        )
        self._res_spin = Gtk.SpinButton(adjustment=adjustment, numeric=True)
        self._res_spin.set_width_chars(5)
        self._res_spin.connect("value-changed", self._on_resolution_changed)
        spin_box.append(self._res_spin)
        dpi_label = Gtk.Label(label="dpi")
        dpi_label.add_css_class("dim-label")
        spin_box.append(dpi_label)
        res_box.append(spin_box)
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.END)
        chips.add_css_class("linked")
        self._res_chips: list[tuple[Gtk.ToggleButton, int]] = []
        for preset in RESOLUTION_PRESETS:
            chip = Gtk.ToggleButton(label=str(preset))
            chip.connect("toggled", self._on_resolution_chip, preset)
            chips.append(chip)
            self._res_chips.append((chip, preset))
        res_box.append(chips)
        self._res_row.add_suffix(res_box)
        self._doc_grp.add(self._res_row)

        self._size_row = Adw.ComboRow(title=_("Page size"))
        self._size_row.set_model(
            Gtk.StringList.new([label for label, _value in PAGE_SIZES])
        )
        self._doc_grp.add(self._size_row)

    def _build_processing_group(self) -> None:
        """Build the Processing group (OCR, language, blank pages)."""
        self._proc_grp = Adw.PreferencesGroup(title=_("Processing"))
        self._ocr_row = Adw.SwitchRow(
            title=_("OCR"),
            subtitle=_("Make the PDF text-searchable (PDF/A)"),
            active=True,
        )
        self._ocr_row.connect("notify::active", self._on_ocr_toggled)
        self._proc_grp.add(self._ocr_row)
        self._lang_row = Adw.ComboRow(title=_("OCR Language"))
        self._lang_row.set_model(
            Gtk.StringList.new([label for label, _value in self._languages])
        )
        self._proc_grp.add(self._lang_row)
        self._blank_row = Adw.SwitchRow(
            title=_("Skip blank pages"),
            subtitle=_("Removes pages detected as empty"),
            active=True,
        )
        self._proc_grp.add(self._blank_row)

    def _build_application_group(self) -> None:
        """Build the Application group (interface language, reset)."""
        self._app_grp = Adw.PreferencesGroup(title=_("Application"))
        self._ui_lang_row = Adw.ComboRow(
            title=_("Interface language"),
            subtitle=_("Only the GUI is translated"),
        )
        self._ui_lang_row.set_model(
            Gtk.StringList.new([label for label, _value in UI_LANGUAGES])
        )
        self._ui_lang_row.connect("notify::selected", self._on_ui_language_changed)
        self._app_grp.add(self._ui_lang_row)

        reset_row = Adw.ActionRow(
            title=_("Reset settings"),
            subtitle=_("Restore all options to their defaults"),
        )
        reset_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        reset_btn.set_child(
            Adw.ButtonContent(
                icon_name="edit-undo-symbolic", label=_("Reset to defaults")
            )
        )
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self._on_reset_clicked)
        reset_row.add_suffix(reset_btn)
        reset_row.set_activatable_widget(reset_btn)
        self._app_grp.add(reset_row)

    def _build_log_area(self) -> None:
        """Build the collapsed, copyable log below the form."""
        self._log_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._log_expander = Gtk.Expander(
            label=_("Log"), hexpand=True, valign=Gtk.Align.CENTER
        )
        log_header.append(self._log_expander)
        copy_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        copy_btn.set_child(
            Adw.ButtonContent(icon_name="edit-copy-symbolic", label=_("Copy"))
        )
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", self._on_copy_log)
        log_header.append(copy_btn)
        self._log_area.append(log_header)
        log_scroller = Gtk.ScrolledWindow(
            min_content_height=170, has_frame=True, visible=False
        )
        self._log_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            left_margin=6,
            right_margin=6,
            top_margin=4,
            bottom_margin=4,
        )
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_buf = self._log_view.get_buffer()
        self._log_end = self._log_buf.create_mark(
            None, self._log_buf.get_end_iter(), False
        )
        log_scroller.set_child(self._log_view)
        self._log_expander.connect(
            "notify::expanded",
            lambda *_a: log_scroller.set_visible(self._log_expander.get_expanded()),
        )
        self._log_area.append(log_scroller)

    def _build_result_bar(self) -> Gtk.Box:
        """Build the persistent bottom bar showing progress and the result."""
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self._status_spinner = Gtk.Spinner(visible=False)
        bar.append(self._status_spinner)
        self._status_icon = Gtk.Image(icon_name="object-select-symbolic", visible=False)
        bar.append(self._status_icon)
        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True
        )
        self._status_title = Gtk.Label(xalign=0.0, label=_("Ready."))
        self._status_title.add_css_class("heading")
        self._status_title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        labels.append(self._status_title)
        self._status_detail = Gtk.Label(xalign=0.0, visible=False)
        self._status_detail.add_css_class("caption")
        self._status_detail.add_css_class("dim-label")
        self._status_detail.add_css_class("monospace")
        self._status_detail.set_ellipsize(3)
        labels.append(self._status_detail)
        bar.append(labels)
        self._show_btn = Gtk.Button(label=_("Show"), visible=False)
        self._show_btn.connect("clicked", lambda *_a: self._show_in_folder())
        bar.append(self._show_btn)
        self._open_btn = Gtk.Button(visible=False)
        self._open_btn.set_child(
            Adw.ButtonContent(icon_name="document-open-symbolic", label=_("Open"))
        )
        self._open_btn.connect("clicked", lambda *_a: self._open_output())
        bar.append(self._open_btn)
        return bar

    def _set_result_bar(
        self,
        state: str,
        title: str,
        detail: str = "",
    ) -> None:
        """Put the bottom bar into ``idle``/``running``/``success``/``error``."""
        self._status_title.set_text(title)
        self._status_detail.set_text(detail)
        self._status_detail.set_visible(bool(detail))
        running = state == "running"
        self._status_spinner.set_visible(running)
        if running:
            self._status_spinner.start()
        else:
            self._status_spinner.stop()
        self._status_icon.set_visible(state in ("success", "error"))
        self._status_icon.set_from_icon_name(
            "dialog-error-symbolic" if state == "error" else "object-select-symbolic"
        )
        if state == "success":
            self._status_icon.add_css_class("success")
        else:
            self._status_icon.remove_css_class("success")
        finished = state == "success" and self._last_output is not None
        self._show_btn.set_visible(finished)
        self._open_btn.set_visible(finished)

    # ------------------------------------------------------- settings I/O

    def _apply_saved_settings(self) -> None:
        """Restore form widgets from the persisted settings."""
        settings = self._settings
        self._source_row.select(str(settings.get("source", "adf-duplex")))
        self._mode_row.select(str(settings.get("mode", "lineart")))
        try:
            resolution = int(str(settings.get("resolution", "300")))
        except ValueError:
            resolution = 300
        self._res_spin.set_value(resolution)
        self._sync_resolution_chips()
        combo_select(self._size_row, PAGE_SIZES, str(settings.get("page_size", "auto")))
        self._ocr_row.set_active(bool(settings.get("ocr", True)))
        self._select_language(str(settings.get("lang", "deu+eng")))
        self._lang_row.set_sensitive(self._ocr_row.get_active())
        self._blank_row.set_active(bool(settings.get("skip_blanks", True)))
        template = str(settings.get("filename_template") or "")
        self._name_entry.set_text(
            "" if template in ("", DEFAULT_OUTPUT_TEMPLATE) else template
        )
        combo_select(
            self._ui_lang_row, UI_LANGUAGES, str(settings.get("ui_language") or "")
        )
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )
        self._on_document_changed()

    def _select_language(self, lang: str) -> None:
        """Select ``lang`` in the language list, adding a custom entry if new."""
        if lang and lang not in [value for _label, value in self._languages]:
            self._languages.append((lang, lang))
            self._lang_row.set_model(
                Gtk.StringList.new([label for label, _value in self._languages])
            )
        for index, (_label, value) in enumerate(self._languages):
            if value == lang:
                self._lang_row.set_selected(index)
                return

    def _save_settings(self) -> None:
        """Snapshot the current form into the settings file."""
        self._settings = {
            "device": self._selected_device() or "",
            "source": self._source_row.value(),
            "mode": self._mode_row.value(),
            "resolution": str(int(self._res_spin.get_value())),
            "page_size": combo_value(self._size_row, PAGE_SIZES),
            "ocr": self._ocr_row.get_active(),
            "lang": self._selected_language(),
            "skip_blanks": self._blank_row.get_active(),
            "filename_template": self._current_template(),
            "ui_language": combo_value(self._ui_lang_row, UI_LANGUAGES),
            "folder": self._folder,
        }
        store_settings(self._settings)

    # ----------------------------------------------------------- devices

    def _refresh_devices(self, *_args: object) -> None:
        """Start an asynchronous ``scanmole --list-devices`` in a worker thread."""
        if self._proc is not None:
            return
        self._refresh_btn.set_sensitive(False)
        self._device_row.set_subtitle(_("Searching for scanners…"))
        prefer = self._selected_device() or str(self._settings.get("device") or "")
        threading.Thread(
            target=self._devices_worker, args=(prefer,), daemon=True
        ).start()

    def _devices_worker(self, prefer: str) -> None:
        """Worker thread: query devices and hand results back to the main loop."""
        if self._cli_version is None:
            self._cli_version = self._probe_cli_version()
        devices: list[dict[str, str]] = []
        err = ""
        try:
            result = subprocess.run(
                [self._scanmole, "--list-devices", "--json"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            for raw in result.stdout.splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("event") == "devices":
                    # Hide virtual devices (webcams, SANE test backend).
                    devices = [
                        device
                        for device in event.get("devices") or []
                        if not device.get("device", "").startswith(("v4l:", "test:"))
                    ]
            if result.stderr.strip():
                GLib.idle_add(self._append_log, result.stderr.strip())
            if result.returncode != 0 and not devices:
                err = _("Device search failed (exit %(code)d).") % {
                    "code": result.returncode
                }
        except FileNotFoundError:
            err = _("scanmole CLI not found — install it or add it to PATH.")
        except subprocess.TimeoutExpired:
            err = _("Device search timed out.")
        except OSError as exc:
            err = _("Device search failed: %(error)s") % {"error": exc}
        GLib.idle_add(self._apply_devices, devices, err, prefer)

    def _probe_cli_version(self) -> str | None:
        """Return the supervised CLI's version string, or ``None``."""
        try:
            result = subprocess.run(
                [self._scanmole, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = result.stdout.strip().splitlines()
        parts = lines[0].split() if lines else []
        return parts[-1] if len(parts) >= 2 else None

    def _apply_devices(
        self, devices: list[dict[str, str]], err: str, prefer: str
    ) -> None:
        """Populate the device dropdown on the main loop."""
        self._refresh_btn.set_sensitive(self._proc is None)
        self._devices = devices
        names = []
        for device in devices:
            vendor = (device.get("vendor") or "").strip()
            model = (device.get("model") or "").strip()
            names.append(
                " ".join(p for p in (vendor, model) if p)
                or device.get("device", _("Unknown device"))
            )
        self._device_row.set_model(Gtk.StringList.new(names))
        # The row itself must stay sensitive either way: disabling it would
        # also disable its refresh-button suffix, leaving no way to rescan.
        if devices:
            index = next(
                (i for i, d in enumerate(devices) if d.get("device") == prefer), 0
            )
            self._device_row.set_selected(index)
            self._device_row.set_subtitle(devices[index].get("device", ""))
            self._set_result_bar(
                "idle",
                ngettext("Found %d scanner.", "Found %d scanners.", len(devices))
                % len(devices),
            )
        else:
            self._device_row.set_subtitle(
                err or _("No scanners found — connect one and press Refresh.")
            )
            self._set_result_bar("idle", _("No scanners found."))
            if err:
                self._append_log(f"[gui] {err}")

    def _selected_device(self) -> str | None:
        """Return the SANE id of the selected device, or ``None``."""
        index = int(self._device_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._devices):
            return self._devices[index].get("device")
        return None

    def _on_device_selected(self, *_args: object) -> None:
        """Show the selected device's SANE id as the row subtitle."""
        self._device_row.set_subtitle(self._selected_device() or "")
        self._update_name_preview()

    # ------------------------------------------------- live consequences

    def _selected_language(self) -> str:
        """Return the Tesseract language code(s) of the selected item."""
        index = int(self._lang_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._languages):
            return self._languages[index][1]
        return self._languages[0][1]

    def _current_template(self) -> str:
        """Return the filename template from the form, with .pdf ensured."""
        template = self._name_entry.get_text().strip() or DEFAULT_OUTPUT_TEMPLATE
        if not template.lower().endswith(".pdf"):
            template += ".pdf"
        return template

    def _current_resolution(self) -> int:
        """Return the dpi from the hybrid resolution control."""
        return int(self._res_spin.get_value())

    def _sync_resolution_chips(self) -> None:
        """Highlight the preset chip matching the entry, or none."""
        self._res_syncing = True
        current = self._current_resolution()
        for chip, preset in self._res_chips:
            chip.set_active(preset == current)
        self._res_syncing = False

    def _on_resolution_chip(self, chip: Gtk.ToggleButton, preset: int) -> None:
        """Fill the entry from a clicked preset chip."""
        if self._res_syncing:
            return
        if chip.get_active():
            self._res_spin.set_value(preset)
        else:
            # Clicking the active chip again keeps it active; the "no chip"
            # state is reached by typing a custom value, not by unselecting.
            self._sync_resolution_chips()

    def _on_resolution_changed(self, *_args: object) -> None:
        """React to any dpi change from the entry or a chip."""
        self._sync_resolution_chips()
        self._on_document_changed()

    def _on_document_changed(self, *_args: object) -> None:
        """Refresh the size estimate and the filename preview."""
        dpi = self._current_resolution()
        base = _SIZE_BASE_MB.get(self._mode_row.value(), 0.3)
        estimate = max(base * (dpi / 300.0) ** 2, 0.1)
        self._res_row.set_subtitle(
            _("%(dpi)d dpi · approx. %(size).1f MB per page")
            % {"dpi": dpi, "size": estimate}
        )
        self._update_name_preview()

    def _update_name_preview(self, *_args: object) -> None:
        """Render the template with the current form values as an example."""
        preset = f"{self._mode_row.value()}-{self._current_resolution()}"
        example = expand_template(
            self._current_template(),
            when=datetime.now().astimezone(),
            counter=1,
            device=self._selected_device() or "device",
            preset=preset,
        )
        self._name_row.set_subtitle(example)

    # ------------------------------------------------------- application

    def _on_ui_language_changed(self, *_args: object) -> None:
        """Persist the interface language and hint at the required restart."""
        value = combo_value(self._ui_lang_row, UI_LANGUAGES)
        self._settings["ui_language"] = value
        store_settings(self._settings)
        # gettext is bound at startup (module-level strings), so a change
        # honestly needs a restart; say so instead of pretending otherwise.
        if value != self._startup_ui_language:
            self._ui_lang_row.set_subtitle(
                _("Only the GUI is translated · takes effect after the next start")
            )
        else:
            self._ui_lang_row.set_subtitle(_("Only the GUI is translated"))

    def _on_reset_clicked(self, *_args: object) -> None:
        """Ask for confirmation, then reset the GUI settings to defaults."""
        dialog = Adw.AlertDialog(
            heading=_("Reset Settings?"),
            body=_(
                "All GUI options return to their defaults. Scanned files and "
                "folders on disk are not touched."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_reset_response)
        dialog.present(self)

    def _on_reset_response(self, _dialog: object, response: str) -> None:
        """Apply the settings reset when confirmed."""
        if response != "reset":
            return
        # Only the GUI settings file is cleared; scans, output folders and
        # the CLI are never touched.
        self._settings = {}
        store_settings(self._settings)
        self._folder = default_folder()
        self._apply_saved_settings()
        self._on_ui_language_changed()
        self._set_result_bar("idle", _("Settings reset to defaults."))

    def _on_about_clicked(self, *_args: object) -> None:
        """Show the About dialog with GUI and CLI versions."""
        cli_version = self._cli_version or _("unknown")
        comments = (
            _("Scan documents from a SANE scanner straight to a searchable PDF.")
            + "\n"
            + _("scanmole CLI: %(version)s") % {"version": cli_version}
        )
        about = Adw.AboutDialog(
            application_name="ScanMole",
            application_icon=APP_ID,
            developer_name="foundata GmbH",
            version=__version__,
            comments=comments,
            website=PROJECT_URL,
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self)

    # ----------------------------------------------------------- scanning

    def _build_argv(self, folder: Path) -> list[str]:
        """Build the ``scanmole --json`` command line from the current form."""
        argv = [self._scanmole, "--json"]
        device = self._selected_device()
        if device:
            argv += ["-d", device]
        argv += [
            "--source",
            self._source_row.value(),
            "--mode",
            self._mode_row.value(),
            "-r",
            str(self._current_resolution()),
            "--page-size",
            combo_value(self._size_row, PAGE_SIZES),
        ]
        if self._ocr_row.get_active():
            argv.append("--ocr")
            argv += ["-l", self._selected_language()]
        else:
            argv.append("--no-ocr")
        if not self._blank_row.get_active():
            argv.append("--keep-blanks")
        # The CLI expands the filename placeholders and picks the next free
        # counter value; the GUI only forwards the template.
        argv += ["-o", str(folder / self._current_template())]
        return argv

    def _on_scan_clicked(self, *_args: object) -> None:
        """Validate the output folder and launch the scan subprocess."""
        if self._proc is not None:
            return
        folder = Path(self._folder).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            return
        self._save_settings()

        self._pages = self._blanks = 0
        self._result = {}
        self._error_message = None
        self._cancelled = False
        self._run_folder = folder
        self._last_output = None

        argv = self._build_argv(folder)
        self._append_log("$ " + shlex.join(argv))
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(folder),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,  # own process group -> clean killpg
            )
        except OSError as exc:
            self._append_log(f"[gui] failed to start scanmole: {exc}")
            self._alert(
                _("Could Not Start scanmole"),
                f"{exc}\n\n" + _("Install the scanmole CLI somewhere in PATH."),
            )
            return
        self._proc = proc
        self._set_result_bar("running", _("Starting scanmole…"))
        self._set_running(True)
        threading.Thread(target=self._supervise, args=(proc,), daemon=True).start()

    def _supervise(self, proc: subprocess.Popen[str]) -> None:
        """Worker thread: pump both pipes, wait, then report back."""
        t_out = threading.Thread(
            target=self._pump,
            args=(proc.stdout, self._on_stdout_line, proc),
            daemon=True,
        )
        t_err = threading.Thread(
            target=self._pump,
            args=(proc.stderr, self._on_stderr_line, proc),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        rc = proc.wait()
        t_out.join(timeout=5)  # pipes hit EOF at process exit
        t_err.join(timeout=5)
        GLib.idle_add(self._on_process_exit, proc, rc)

    @staticmethod
    def _pump(
        stream: IO[str] | None,
        handler: Callable[[subprocess.Popen[str], str], object],
        proc: subprocess.Popen[str],
    ) -> None:
        """Forward each line from ``stream`` to ``handler`` on the main loop."""
        if stream is None:  # unreachable: the child is started with PIPE
            return
        try:
            for line in stream:
                GLib.idle_add(handler, proc, line)
        except (OSError, ValueError):
            pass  # the pipe went away with the process; exit reporting covers it

    # -------------------------------------------------- JSON event stream

    def _on_stdout_line(self, proc: object, line: str) -> None:
        """Interpret one JSON event line from the CLI."""
        if proc is not self._proc:
            return  # stale run
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            self._append_log(line)  # non-JSON on stdout: just log it
            return
        kind = event.get("event")
        if kind == "start":
            self._set_result_bar("running", _("Scanning…"))
        elif kind == "page":
            self._pages = as_int(event.get("n"), self._pages + 1)
            if event.get("blank") and self._blank_row.get_active():
                self._blanks += 1
            text = _("Page %d scanned") % self._pages
            if self._blanks:
                text += (
                    ngettext(
                        " (%d blank skipped)", " (%d blanks skipped)", self._blanks
                    )
                    % self._blanks
                )
            self._set_result_bar("running", text + "…")
        elif kind == "scan_done":
            total = as_int(event.get("total"), self._pages)
            kept = as_int(event.get("kept"), max(self._pages - self._blanks, 0))
            self._set_result_bar(
                "running",
                ngettext(
                    "Scan finished — keeping %(kept)d of %(total)d page…",
                    "Scan finished — keeping %(kept)d of %(total)d pages…",
                    total,
                )
                % {"kept": kept, "total": total},
            )
        elif kind == "ocr_start":
            self._set_result_bar("running", _("Running OCR…"))
        elif kind == "done":
            self._result = event
        elif kind == "error":
            self._error_message = str(event.get("message") or _("Unknown error"))
            self._append_log(f"[error] {self._error_message}")

    def _on_stderr_line(self, _proc: object, line: str) -> None:
        """Append a raw stderr line to the log view."""
        line = line.rstrip("\n")
        if line:
            self._append_log(line)

    # ------------------------------------------------------- process exit

    def _on_process_exit(self, proc: object, rc: int) -> None:
        """Finalize the UI when the scan subprocess exits."""
        if proc is not self._proc:
            return
        self._proc = None
        self._set_running(False)
        self._append_log(f"[gui] scanmole exited with code {rc}")

        if self._cancelled:
            self._set_result_bar("idle", _("Scan cancelled."))
            return
        if rc == 0:
            output = self._result.get("output")
            pages = as_int(self._result.get("pages"), self._pages)
            if output:
                path = Path(str(output))
                if not path.is_absolute():
                    path = self._run_folder / path
                self._last_output = path
                summary = ngettext("%d page saved", "%d pages saved", pages) % pages
                if self._blanks:
                    summary += " · " + (
                        ngettext("%d blank skipped", "%d blanks skipped", self._blanks)
                        % self._blanks
                    )
                self._set_result_bar("success", summary, path.name)
            else:
                self._set_result_bar("idle", _("Finished."))
            return
        heading, body = EXIT_HINTS.get(
            rc,
            (
                _("Scan Failed"),
                _("scanmole exited with status %(code)d. See the log for details.")
                % {"code": rc},
            ),
        )
        if self._error_message:
            body = body + "\n\n" + _("Details:") + " " + self._error_message
        self._set_result_bar("error", heading)
        self._alert(heading, body)

    # ------------------------------------------------------------- cancel

    def _on_cancel_clicked(self, *_args: object) -> None:
        """Terminate the scan's process group, escalating to SIGKILL."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._cancelled = True
        self._cancel_btn.set_sensitive(False)
        self._set_result_bar("running", _("Cancelling…"))
        self._append_log("[gui] cancelling — SIGTERM to process group")
        self._signal_group(proc, signal.SIGTERM)
        GLib.timeout_add_seconds(SIGKILL_GRACE_SECONDS, self._sigkill_if_alive, proc)

    def _sigkill_if_alive(self, proc: subprocess.Popen[str]) -> bool:
        """SIGKILL the process group if it survived the SIGTERM grace period."""
        if proc.poll() is None:
            self._append_log("[gui] still running — SIGKILL to process group")
            self._signal_group(proc, signal.SIGKILL)
        return bool(GLib.SOURCE_REMOVE)

    @staticmethod
    def _signal_group(proc: subprocess.Popen[str], sig: int) -> None:
        """Send ``sig`` to the subprocess's process group (pid == pgid)."""
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _on_close_request(self, *_args: object) -> bool:
        """Terminate any running scan before letting the window close."""
        if self._proc is not None and self._proc.poll() is None:
            self._signal_group(self._proc, signal.SIGTERM)
        return False  # allow the window to close

    # -------------------------------------------------------- UI plumbing

    def _set_running(self, running: bool) -> None:
        """Toggle the form and the primary action for a running scan."""
        self._scan_btn.set_visible(not running)
        self._cancel_btn.set_visible(running)
        self._cancel_btn.set_sensitive(True)
        self._refresh_btn.set_sensitive(not running)
        for group in self._form_groups:
            group.set_sensitive(not running)
        # The Scanner group hosts the Scan/Cancel buttons; keep them usable
        # while the rest of the group is locked during a run.
        if running:
            self._scanner_grp.set_sensitive(True)
            self._device_row.set_sensitive(False)
            self._source_row.row.set_sensitive(False)
        else:
            self._device_row.set_sensitive(True)
            self._source_row.row.set_sensitive(True)

    def _append_log(self, text: str) -> None:
        """Append a line to the log view and scroll it into view."""
        self._log_buf.insert(self._log_buf.get_end_iter(), text.rstrip("\n") + "\n")
        self._log_view.scroll_to_mark(self._log_end, 0.0, False, 0.0, 1.0)

    def _on_copy_log(self, *_args: object) -> None:
        """Copy the whole log text to the clipboard."""
        start, end = self._log_buf.get_bounds()
        text = self._log_buf.get_text(start, end, True)
        provider = Gdk.ContentProvider.new_for_value(text)
        self.get_clipboard().set_content(provider)

    def _alert(self, heading: str, body: str) -> None:
        """Present a simple modal alert dialog."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def _on_ocr_toggled(self, *_args: object) -> None:
        """Enable the language selection only while OCR is on."""
        self._lang_row.set_sensitive(self._ocr_row.get_active())

    def _on_pick_folder(self, *_args: object) -> None:
        """Open a folder chooser for the output directory."""
        dialog = Gtk.FileDialog(title=_("Choose Output Folder"), modal=True)
        if self._folder and Path(self._folder).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(self._folder))
        dialog.select_folder(self, None, self._on_folder_picked)

    def _on_folder_picked(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        """Store the chosen output folder, ignoring a dismissed dialog."""
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self._folder = gfile.get_path()
            self._folder_btn.set_child(
                Adw.ButtonContent(
                    icon_name="folder-symbolic", label=abbreviate_home(self._folder)
                )
            )

    def _open_output(self) -> None:
        """Open the produced PDF in the default application."""
        if self._last_output is None:
            return
        uri = Gio.File.new_for_path(str(self._last_output)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            subprocess.Popen(
                ["xdg-open", str(self._last_output)], start_new_session=True
            )

    def _show_in_folder(self) -> None:
        """Reveal the produced PDF in the file manager."""
        if self._last_output is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self._last_output)))
        launcher.open_containing_folder(self, None, None)


# Same GTK boundary as MainWindow: the base class is Any without stubs.
class ScanMoleApp(Adw.Application):  # type: ignore[misc]
    """The libadwaita application owning a single :class:`MainWindow`."""

    def __init__(self) -> None:
        """Register the activation handler."""
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: Adw.Application) -> None:
        """Present the main window, creating it on first activation."""
        # Make the packaged mascot icon resolvable by name (About dialog).
        display = Gdk.Display.get_default()
        if display is not None and ICON_DIR.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(ICON_DIR))
        win = self.props.active_window or MainWindow(application=app)
        win.present()


def main(argv: list[str] | None = None) -> int:
    """Run the ScanMole GUI and return the application exit code."""
    GLib.set_application_name("ScanMole")
    app = ScanMoleApp()
    return int(app.run(sys.argv if argv is None else argv))  # untyped GTK call


if __name__ == "__main__":
    raise SystemExit(main())
