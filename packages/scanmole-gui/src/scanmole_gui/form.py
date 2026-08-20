"""The scan form: widgets and form-local behavior, no orchestration.

Owns the Scanner, Output, Document and Processing groups, their local
consequences (dependent sensitivity, the resolution control, the live
filename preview, the OCR language list) and the snapshotting of form
values for persistence and the immutable :class:`ScanRequest`. Events
that need orchestration (device/source changes, scan, cancel, refresh,
folder picking, opening dialogs) leave through explicit callbacks; the
window owns workers, the capability flow, the runner and every dialog.
Capability-derived information enters prepared through small callables
instead of engine imports.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402  # after require_version

# The GUI holds no pipeline logic; the pure naming helper is imported only so
# the live filename preview matches what the CLI will produce.
from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE, expand_template  # noqa: E402
from scanmole_gui.i18n import _  # noqa: E402  # after gi setup
from scanmole_gui.modes import SCAN_MODES  # noqa: E402
from scanmole_gui.probing import SOURCE_VALUES  # noqa: E402
from scanmole_gui.request import ScanRequest  # noqa: E402
from scanmole_gui.widgets import (  # noqa: E402
    ChoiceRow,
    combo_select,
    combo_value,
    plain_string_factory,
)

SOURCES = (
    (_("Flatbed"), "flatbed"),
    (_("ADF"), "adf"),
    (_("ADF Duplex"), "adf-duplex"),
    (_("ADF Back"), "adf-back"),
)
SOURCE_TOOLTIPS = (
    _("Single page from the flatbed glass"),
    _("Automatic Document Feeder — front sides only"),
    _("Automatic Document Feeder — both sides of every sheet"),
    _("Automatic Document Feeder — back sides only"),
)
# Literal labels: xgettext only extracts literal _() calls, so building
# this from SCAN_MODES would silently drop the labels from the catalog.
MODES = (
    (_("B/W"), "lineart"),
    (_("Gray"), "gray"),
    (_("Color"), "color"),
    (_("B/W (faint)"), "lineart-auto"),
)
if tuple(value for _label, value in MODES) != tuple(
    value for _label, value in SCAN_MODES
):  # pragma: no cover -- import-time consistency guard
    raise RuntimeError("MODES and scanmole_gui.modes.SCAN_MODES diverged")
if tuple(value for _label, value in SOURCES) != SOURCE_VALUES:
    # pragma: no cover -- import-time consistency guard
    raise RuntimeError("SOURCES and scanmole_gui.probing.SOURCE_VALUES diverged")
MODE_TOOLTIPS = (
    _("Black and white (1-bit)"),
    "",
    "",
    _(
        "Black and White (1-bit) for faint originals "
        "(e.g., thermal-paper receipts, washed-out copies)"
    ),
)
RESOLUTION_PRESETS = (200, 250, 300, 600)
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
# The tie-break family for ambiguous automatic sizes (A4 and Letter often
# both fit the content). A preference, never a restriction; the CLI value
# is carried alongside the label, so behavior never parses translations.
AUTO_SIZE_PREFERENCES = (
    (_("ISO (A sizes)"), "iso"),
    (_("North America (Letter/Legal)"), "north-american"),
)
LANGUAGES = (
    (_("German (deu)"), "deu"),
    (_("English (eng)"), "eng"),
    (_("German + English (deu+eng)"), "deu+eng"),
)

# Rough size per page at 300 dpi, from measured fleet scans; scaled by dpi².
# Content-dependent, so only ever presented as an approximation.
_SIZE_BASE_MB = {"lineart": 0.1, "lineart-auto": 0.1, "gray": 0.3, "color": 0.5}


def default_folder() -> str:
    """Return the XDG Documents folder, falling back to the home directory."""
    docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    return str(docs) if docs else str(Path.home())


def abbreviate_home(path: str) -> str:
    """Render a path with the home directory abbreviated to ``~``."""
    import os

    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


class ScanForm:
    """The scan form component: four preference groups plus Scan/Cancel.

    The window composes the group widgets into its responsive layout and
    receives orchestration events through the constructor callbacks; all
    other signal handling is form-local.
    """

    def __init__(
        self,
        *,
        on_device_selected: Callable[[], None],
        on_source_changed: Callable[[bool], None],
        on_refresh: Callable[[], None],
        on_scan: Callable[[], None],
        on_cancel: Callable[[], None],
        on_pick_folder: Callable[[], None],
        on_more_languages: Callable[[], None],
        on_choice_blocked: Callable[[str, str], None],
        device_for_preview: Callable[[], str | None],
        effective_resolution: Callable[[int], int | None],
    ) -> None:
        """Build the four groups; ``apply_settings`` restores the values.

        ``device_for_preview`` supplies the selected device id for the
        filename preview; ``effective_resolution`` returns the dpi the
        device would actually scan at when it differs (a prepared
        capability assessment, so the form needs no engine imports).
        """
        self._on_device_selected = on_device_selected
        self._on_source_changed = on_source_changed
        self._on_refresh = on_refresh
        self._on_scan = on_scan
        self._on_cancel = on_cancel
        self._on_pick_folder = on_pick_folder
        self._on_more_languages = on_more_languages
        self._on_choice_blocked = on_choice_blocked
        self._device_for_preview = device_for_preview
        self._effective_resolution = effective_resolution

        self._folder = default_folder()
        self._languages: list[tuple[str, str]] = list(LANGUAGES)
        self._current_language = "deu+eng"
        self._reconciling_source = False
        self._res_syncing = False
        self._res_value = 300

        self._build_scanner_group()
        self._build_output_group()
        self._build_document_group()
        self._build_processing_group()
        self._equalize_form_rows()
        # The initial hint/preview render happens via refresh_document_hints()
        # once the window finished wiring; the preview callback reaches back
        # into the controller, which must hold the form reference by then.

    # ------------------------------------------------------------ building

    def _build_scanner_group(self) -> None:
        """Build the Scanner group including the primary Scan action."""
        self.scanner_group = Adw.PreferencesGroup(title=_("Scanner"))
        # Never make this row insensitive: the refresh button is one of its
        # suffix children, and a disabled row would take the only way to
        # recover from an empty device list down with it.
        self._device_row = Adw.ComboRow(
            title=_("Device"),
            subtitle=_("Searching for scanners…"),
        )
        self._device_row.set_factory(plain_string_factory())
        self._device_row.connect("notify::selected", self._device_selected)
        # The rescan action sits beside the device list, not in the header:
        # the action lives where its object is (mockup rule).
        self._refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Refresh devices"),
        )
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", lambda *_a: self._on_refresh())
        self._device_row.add_suffix(self._refresh_btn)
        self.scanner_group.add(self._device_row)
        self._source_row = ChoiceRow(
            self.scanner_group,
            _("Source"),
            SOURCES,
            self._source_changed,
            tooltips=SOURCE_TOOLTIPS,
        )

        # One primary action: Scan is the only accented control, full width at
        # the bottom of the Scanner group (mockup rule); Cancel swaps in while
        # a scan runs. The buttons are wrapped in list rows because a plain
        # widget given to PreferencesGroup.add() lands below the card, not in
        # it.
        self._scan_btn = Gtk.Button(
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8
        )
        self._scan_btn.set_child(
            Adw.ButtonContent(
                icon_name="media-playback-start-symbolic", label=_("Scan")
            )
        )
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.add_css_class("pill")
        self._scan_btn.connect("clicked", lambda *_a: self._on_scan())
        self._scan_row = Gtk.ListBoxRow(
            child=self._scan_btn, activatable=False, selectable=False
        )
        self.scanner_group.add(self._scan_row)
        self._cancel_btn = Gtk.Button(
            label=_("Cancel"),
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.add_css_class("pill")
        self._cancel_btn.connect("clicked", lambda *_a: self._on_cancel())
        self._cancel_row = Gtk.ListBoxRow(
            child=self._cancel_btn, activatable=False, selectable=False, visible=False
        )
        self.scanner_group.add(self._cancel_row)

    def _build_output_group(self) -> None:
        """Build the Output group (folder, filename template)."""
        self.output_group = Adw.PreferencesGroup(title=_("Output"))
        self._folder_row = Adw.ActionRow(title=_("Folder"))
        self._folder_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )
        self._folder_btn.connect("clicked", lambda *_a: self._on_pick_folder())
        self._folder_row.add_suffix(self._folder_btn)
        self._folder_row.set_activatable_widget(self._folder_btn)
        self.output_group.add(self._folder_row)

        # Deliberately about twice a default row: entry, placeholder helper
        # and preview stack so the Output group lines up with the Scanner
        # group's Device/Source/Scan rows in the two-column grid. A custom
        # row (not Adw.ActionRow) so the title can align with the entry at
        # the top instead of centering over the whole stack.
        name_row_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=17,
            margin_start=12,
            margin_end=12,
        )
        name_title = Gtk.Label(
            label=_("File name"),
            xalign=0.0,
            valign=Gtk.Align.START,
            margin_top=18,
        )
        name_row_box.append(name_title)
        name_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
            hexpand=True,
            margin_top=10,
            margin_bottom=10,
        )
        self._name_entry = Gtk.Entry(
            placeholder_text=DEFAULT_OUTPUT_TEMPLATE, width_chars=34, hexpand=True
        )
        self._name_entry.connect("changed", self._update_name_preview)
        # Focusing the empty field materializes the default template so it
        # can be edited instead of retyped; leaving it unchanged still counts
        # as "use the default" (the save logic normalizes it back).
        name_focus = Gtk.EventControllerFocus()
        name_focus.connect("enter", self._on_name_entry_focus)
        self._name_entry.add_controller(name_focus)
        name_box.append(self._name_entry)
        hint = Gtk.Label(
            label=_(
                "Placeholders: {YYYY} {MM} {DD} {hh} {mm} {ss} {device}\n"
                "{N} (auto-no., 0-padded, repeatable)"
            ),
            xalign=1.0,
            wrap=True,
            justify=Gtk.Justification.RIGHT,
            max_width_chars=44,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        name_box.append(hint)
        self._name_preview = Gtk.Label(xalign=1.0)
        self._name_preview.add_css_class("caption")
        self._name_preview.add_css_class("dim-label")
        self._name_preview.set_ellipsize(3)  # Pango.EllipsizeMode.END
        name_box.append(self._name_preview)
        name_row_box.append(name_box)
        self._name_row = Gtk.ListBoxRow(
            child=name_row_box, activatable=False, selectable=False
        )
        self.output_group.add(self._name_row)

    def _build_document_group(self) -> None:
        """Build the Document group (color mode, page size, resolution)."""
        self.document_group = Adw.PreferencesGroup(title=_("Document"))
        self._mode_row = ChoiceRow(
            self.document_group,
            _("Color mode"),
            MODES,
            self._on_document_changed,
            tooltips=MODE_TOOLTIPS,
        )
        # Page size plus the automatic-size family preference in one row:
        # the second dropdown only matters (and is only sensitive) while
        # the first says Automatic; it keeps its value while disabled.
        self._size_row = Adw.ActionRow(title=_("Page size"))
        size_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        self._size_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _value in PAGE_SIZES])
        )
        self._size_dropdown.set_factory(plain_string_factory())
        self._size_dropdown.connect("notify::selected", self._on_page_size_changed)
        size_box.append(self._size_dropdown)
        self._size_pref_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _value in AUTO_SIZE_PREFERENCES])
        )
        self._size_pref_dropdown.set_factory(plain_string_factory())
        self._size_pref_dropdown.set_tooltip_text(
            _("Resolves ambiguous automatic page sizes (A4 and Letter often both fit).")
        )
        self._size_pref_dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Automatic size preference")]
        )
        self._size_pref_dropdown.connect("notify::selected", self._on_document_changed)
        size_box.append(self._size_pref_dropdown)
        self._size_row.add_suffix(size_box)
        self.document_group.add(self._size_row)

        # Hybrid resolution control, composed as entry / unit / stepper so
        # the unit sits between the number and the buttons (GtkSpinButton
        # cannot render a unit suffix). The static bounds are a sanity clamp
        # only; the CLI snaps the value to what the device actually supports
        # during capability negotiation.
        self._res_row = Adw.ActionRow(title=_("Resolution"))
        entry_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        self._res_entry = Gtk.Entry(
            text="300",
            width_chars=4,
            max_width_chars=4,
            max_length=4,
            xalign=1.0,
            input_purpose=Gtk.InputPurpose.DIGITS,
        )
        self._res_entry.add_css_class("dpi")
        self._res_entry.connect("activate", self._on_resolution_commit)
        res_focus = Gtk.EventControllerFocus()
        res_focus.connect("leave", self._on_resolution_commit)
        self._res_entry.add_controller(res_focus)
        res_keys = Gtk.EventControllerKey()
        res_keys.connect("key-pressed", self._on_resolution_key)
        self._res_entry.add_controller(res_keys)
        res_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        res_scroll.connect("scroll", self._on_resolution_scroll)
        self._res_entry.add_controller(res_scroll)
        entry_box.append(self._res_entry)
        dpi_label = Gtk.Label(label="dpi")
        dpi_label.add_css_class("dim-label")
        entry_box.append(dpi_label)
        stepper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        stepper.add_css_class("linked")
        minus_btn = Gtk.Button(icon_name="list-remove-symbolic")
        minus_btn.connect("clicked", lambda *_a: self._step_resolution(-10))
        stepper.append(minus_btn)
        plus_btn = Gtk.Button(icon_name="list-add-symbolic")
        plus_btn.connect("clicked", lambda *_a: self._step_resolution(10))
        stepper.append(plus_btn)
        entry_box.append(stepper)
        self._res_row.add_suffix(entry_box)
        self._res_row.add_css_class("joined-below")
        self.document_group.add(self._res_row)

        # The dpi presets get their own row directly below Resolution: the
        # split gives Document the same four-row shape as Processing, which
        # _equalize_form_rows() then locks to identical heights.
        self._chips_row = Adw.ActionRow()
        self._chips_row.add_css_class("joined-above")
        chips = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        chips.add_css_class("linked")
        self._res_chips: list[tuple[Gtk.ToggleButton, int]] = []
        for preset in RESOLUTION_PRESETS:
            chip = Gtk.ToggleButton(label=str(preset))
            chip.add_css_class("chip")
            chip.connect("toggled", self._on_resolution_chip, preset)
            chips.append(chip)
            self._res_chips.append((chip, preset))
        self._chips_row.add_suffix(chips)
        self.document_group.add(self._chips_row)

    def _build_processing_group(self) -> None:
        """Build the Processing group (blank pages, OCR, language)."""
        self.processing_group = Adw.PreferencesGroup(title=_("Processing"))
        self._blank_row = Adw.SwitchRow(
            title=_("Skip blank pages"),
            subtitle=_("Removes pages detected as empty"),
            active=True,
        )
        self.processing_group.add(self._blank_row)
        self._ocr_row = Adw.SwitchRow(
            title=_("OCR"),
            subtitle=_("Make the PDF text-searchable (PDF/A)"),
            active=True,
        )
        self._ocr_row.connect("notify::active", self._on_ocr_toggled)
        self.processing_group.add(self._ocr_row)
        self._lang_row = Adw.ComboRow(title=_("OCR Language"))
        # Same 20-character default-factory cap as the device row: without a
        # plain-label factory, "German + English (deu+eng)" gets ellipsized.
        self._lang_row.set_factory(plain_string_factory())
        self._set_language_model()
        self._lang_row.connect("notify::selected", self._on_language_selected)
        self.processing_group.add(self._lang_row)
        self._deskew_row = Adw.SwitchRow(
            title=_("Deskew"),
            subtitle=_("Correct skewed scanned pages"),
            active=True,
        )
        self.processing_group.add(self._deskew_row)

    def _equalize_form_rows(self) -> None:
        """Lock Document and Processing to the same height, row by row.

        Both cards have four rows; a vertical size group per cross-column
        pair makes the sections end flush, with the resolution entry and
        preset rows together exactly as tall as OCR language plus deskew.
        The groups must outlive this method (widgets do not reference them).
        """
        self._row_size_groups: list[Gtk.SizeGroup] = []
        for left, right in (
            (self._mode_row.row, self._blank_row),
            (self._size_row, self._ocr_row),
            (self._res_row, self._lang_row),
            (self._chips_row, self._deskew_row),
        ):
            size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)
            size_group.add_widget(left)
            size_group.add_widget(right)
            self._row_size_groups.append(size_group)

    # ------------------------------------------------------------- devices

    def show_devices(self, names: list[str], selected_index: int) -> None:
        """Populate the device dropdown and select ``selected_index``."""
        self._device_row.set_model(Gtk.StringList.new(names))
        if names:
            self._device_row.set_selected(selected_index)

    def device_index(self) -> int:
        """The selected index in the device dropdown."""
        return int(self._device_row.get_selected())  # untyped GTK call

    def set_device_subtitle(self, text: str) -> None:
        """Show a status line under the device selector."""
        self._device_row.set_subtitle(text)

    def set_device_tooltip(self, text: str) -> None:
        """Expose the selected device's SANE id as a tooltip.

        As a tooltip (not a subtitle) the diagnostic id costs no row width,
        so the selected model name renders without ellipses.
        """
        self._device_row.set_tooltip_text(text)

    def _device_selected(self, *_args: object) -> None:
        self._update_name_preview()
        self._on_device_selected()

    # ----------------------------------------------------- source and mode

    def _source_changed(self) -> None:
        """Report a source change with the manual/programmatic context.

        Whether the change is manual (a preference) or programmatic (a
        reconciliation select) is widget-callback context only the form
        has; the GTK-free flow owns everything else.
        """
        self._on_source_changed(not self._reconciling_source)

    def source_value(self) -> str:
        """The CLI value of the selected source."""
        return self._source_row.value()

    def select_source(self, value: str) -> None:
        """Select a source programmatically (a flow reconciliation)."""
        self._reconciling_source = True
        try:
            self._source_row.select(value)
        finally:
            self._reconciling_source = False

    def mode_value(self) -> str:
        """The CLI value of the selected color mode."""
        return self._mode_row.value()

    def set_source_availability(self, blocked: dict[str, str]) -> None:
        """Render which sources are selectable (value -> blocking reason)."""
        self._source_row.set_availability(blocked, self._on_choice_blocked)

    def set_mode_availability(self, blocked: dict[str, str]) -> None:
        """Render which modes are selectable (value -> blocking reason)."""
        self._mode_row.set_availability(blocked, self._on_choice_blocked)

    def selection_blocked_reason(self) -> str | None:
        """Why the current source or mode selection is unavailable, if so."""
        return self._source_row.blocked_reason() or self._mode_row.blocked_reason()

    # ---------------------------------------------------------- languages

    def _set_language_model(self) -> None:
        """Rebuild the language dropdown: known languages plus "Add more…"."""
        labels = [label for label, _value in self._languages]
        labels.append(_("Add more…"))
        self._lang_row.set_model(Gtk.StringList.new(labels))

    def select_language(self, lang: str) -> None:
        """Select ``lang`` in the language list, adding a custom entry if new."""
        if lang and lang not in [value for _label, value in self._languages]:
            self._languages.append((lang, lang))
            self._set_language_model()
        for index, (_label, value) in enumerate(self._languages):
            if value == lang:
                self._lang_row.set_selected(index)
                self._current_language = lang
                return

    def _on_language_selected(self, *_args: object) -> None:
        """Track real selections; "Add more…" opens the language help."""
        index = int(self._lang_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._languages):
            self._current_language = self._languages[index][1]
            return
        # The "Add more…" pseudo item: revert to the previous selection and
        # explain how additional Tesseract languages are managed.
        self.select_language(self._current_language)
        self._on_more_languages()

    def selected_language(self) -> str:
        """Return the Tesseract language code(s) of the selected item."""
        index = int(self._lang_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._languages):
            return self._languages[index][1]
        return self._current_language  # "Add more…" is never a language

    def _on_ocr_toggled(self, *_args: object) -> None:
        """Enable the language selection only while OCR is on."""
        self._lang_row.set_sensitive(self._ocr_row.get_active())

    # ---------------------------------------------------------- resolution

    def resolution(self) -> int:
        """Return the dpi from the hybrid resolution control."""
        return self._res_value

    def _set_resolution(self, value: int) -> None:
        """Clamp and apply a dpi value to the entry, chips and previews."""
        value = max(RESOLUTION_MINIMUM, min(RESOLUTION_MAXIMUM, value))
        self._res_value = value
        if self._res_entry.get_text() != str(value):
            self._res_entry.set_text(str(value))
            self._res_entry.set_position(-1)
        self._sync_resolution_chips()
        self._on_document_changed()

    def _on_resolution_commit(self, *_args: object) -> None:
        """Validate the typed dpi on Enter or focus-out; revert if invalid."""
        text = self._res_entry.get_text().strip()
        try:
            self._set_resolution(int(text))
        except ValueError:
            self._set_resolution(self._res_value)

    def _step_resolution(self, delta: int) -> None:
        """Step the dpi by ``delta`` from the stepper, keys or scroll wheel."""
        self._on_resolution_commit()  # honor a value typed but not committed
        self._set_resolution(self._res_value + delta)

    def _on_resolution_key(
        self, _controller: object, keyval: int, _keycode: int, _state: object
    ) -> bool:
        """Handle Up/Down arrows in the dpi entry."""
        if keyval == Gdk.KEY_Up:
            self._step_resolution(10)
            return True
        if keyval == Gdk.KEY_Down:
            self._step_resolution(-10)
            return True
        return False

    def _on_resolution_scroll(self, _controller: object, _dx: float, dy: float) -> bool:
        """Handle scroll-wheel stepping over the dpi entry."""
        self._step_resolution(-10 if dy > 0 else 10)
        return True

    def _sync_resolution_chips(self) -> None:
        """Highlight the preset chip matching the entry, or none."""
        self._res_syncing = True
        current = self.resolution()
        for chip, preset in self._res_chips:
            chip.set_active(preset == current)
        self._res_syncing = False

    def _on_resolution_chip(self, chip: Gtk.ToggleButton, preset: int) -> None:
        """Fill the entry from a clicked preset chip."""
        if self._res_syncing:
            return
        if chip.get_active():
            self._set_resolution(preset)
        else:
            # Clicking the active chip again keeps it active; the "no chip"
            # state is reached by typing a custom value, not by unselecting.
            self._sync_resolution_chips()

    # --------------------------------------------------- live consequences

    def _on_page_size_changed(self, *_args: object) -> None:
        """Gate the family preference: it only applies in automatic mode."""
        automatic = combo_value(self._size_dropdown, PAGE_SIZES) == "auto"
        self._size_pref_dropdown.set_sensitive(automatic)
        self._on_document_changed()

    def refresh_document_hints(self) -> None:
        """Re-render the size estimate and hints (e.g. new capabilities)."""
        self._on_document_changed()

    def _on_document_changed(self, *_args: object) -> None:
        """Refresh the size estimate and the filename preview."""
        dpi = self.resolution()
        base = _SIZE_BASE_MB.get(self._mode_row.value(), 0.3)
        estimate = max(base * (dpi / 300.0) ** 2, 0.1)
        effective = self._effective_resolution(dpi)
        if effective is not None and effective != dpi:
            self._res_row.set_subtitle(
                _(
                    "%(dpi)d dpi · scans at %(effective)d dpi · "
                    "approx. %(size).1f MB per page"
                )
                % {
                    "dpi": dpi,
                    "effective": effective,
                    "size": max(base * (effective / 300.0) ** 2, 0.1),
                }
            )
            self._update_name_preview()
            return
        self._res_row.set_subtitle(
            _("%(dpi)d dpi · approx. %(size).1f MB per page")
            % {"dpi": dpi, "size": estimate}
        )
        self._update_name_preview()

    def _on_name_entry_focus(self, *_args: object) -> None:
        """Prefill the default template on focus so it can be edited."""
        if not self._name_entry.get_text():
            self._name_entry.set_text(DEFAULT_OUTPUT_TEMPLATE)
            self._name_entry.set_position(-1)

    def _update_name_preview(self, *_args: object) -> None:
        """Render the template with the current form values as an example."""
        example = expand_template(
            self._current_template(),
            when=datetime.now().astimezone(),
            counter=1,
            device=self._device_for_preview() or "device",
        )
        self._name_preview.set_text(_("Preview: %(name)s") % {"name": example})

    def _current_template(self) -> str:
        """Return the filename template from the form, with .pdf ensured."""
        template = self._name_entry.get_text().strip() or DEFAULT_OUTPUT_TEMPLATE
        if not template.lower().endswith(".pdf"):
            template += ".pdf"
        return template

    # -------------------------------------------------------------- folder

    def folder(self) -> str:
        """The output folder currently shown on the folder button."""
        return self._folder

    def set_folder(self, path: str) -> None:
        """Adopt a picked output folder and refresh the button label."""
        self._folder = path
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )

    # ----------------------------------------------------------- snapshots

    def apply_settings(self, settings: dict[str, object]) -> None:
        """Restore the form widgets from the persisted settings."""
        self._source_row.select(str(settings.get("source", "adf-duplex")))
        self._mode_row.select(str(settings.get("mode", "lineart")))
        try:
            resolution = int(str(settings.get("resolution", "300")))
        except ValueError:
            resolution = 300
        self._set_resolution(resolution)
        combo_select(
            self._size_dropdown, PAGE_SIZES, str(settings.get("page_size", "auto"))
        )
        # A missing or unknown saved value keeps the default (index 0: ISO).
        combo_select(
            self._size_pref_dropdown,
            AUTO_SIZE_PREFERENCES,
            str(settings.get("auto_size_preference", "iso")),
        )
        self._on_page_size_changed()
        self._ocr_row.set_active(bool(settings.get("ocr", True)))
        self._deskew_row.set_active(bool(settings.get("deskew", True)))
        self.select_language(str(settings.get("lang", "deu+eng")))
        self._lang_row.set_sensitive(self._ocr_row.get_active())
        self._blank_row.set_active(bool(settings.get("skip_blanks", True)))
        template = str(settings.get("filename_template") or "")
        self._name_entry.set_text(
            "" if template in ("", DEFAULT_OUTPUT_TEMPLATE) else template
        )
        self.set_folder(str(settings.get("folder") or default_folder()))
        self._on_document_changed()

    def persisted_values(self) -> dict[str, object]:
        """Snapshot the form-owned values for the settings file.

        The window adds what only it knows: the selected device id, the
        user's preferred source (not a temporary sole-source adoption)
        and the window geometry.
        """
        return {
            "mode": self._mode_row.value(),
            "resolution": str(self.resolution()),
            "page_size": combo_value(self._size_dropdown, PAGE_SIZES),
            "auto_size_preference": combo_value(
                self._size_pref_dropdown, AUTO_SIZE_PREFERENCES
            ),
            "ocr": self._ocr_row.get_active(),
            "deskew": self._deskew_row.get_active(),
            "lang": self.selected_language(),
            "skip_blanks": self._blank_row.get_active(),
            "filename_template": self._current_template(),
            "folder": self._folder,
        }

    def scan_request(self, device: str | None, folder: Path) -> ScanRequest:
        """Snapshot the form into an immutable scan request."""
        return ScanRequest(
            device=device,
            source=self._source_row.value(),
            mode=self._mode_row.value(),
            resolution=self.resolution(),
            page_size=combo_value(self._size_dropdown, PAGE_SIZES),
            auto_size_preference=(
                "north-american"
                if combo_value(self._size_pref_dropdown, AUTO_SIZE_PREFERENCES)
                == "north-american"
                else "iso"
            ),
            ocr=bool(self._ocr_row.get_active()),
            lang=self.selected_language(),
            deskew=bool(self._deskew_row.get_active()),
            drop_blanks=bool(self._blank_row.get_active()),
            # The CLI expands the filename placeholders and picks the next
            # free counter value; the GUI only forwards the template.
            output=str(folder / self._current_template()),
        )

    # ------------------------------------------------------- running state

    def set_running(self, running: bool) -> None:
        """Toggle the form and the primary action for a running scan."""
        self._scan_row.set_visible(not running)
        self._cancel_row.set_visible(running)
        self._cancel_btn.set_sensitive(True)
        self._refresh_btn.set_sensitive(not running)
        for group in (
            self.scanner_group,
            self.document_group,
            self.processing_group,
            self.output_group,
        ):
            group.set_sensitive(not running)
        # The Scanner group hosts the Scan/Cancel buttons; keep them usable
        # while the rest of the group is locked during a run.
        if running:
            self.scanner_group.set_sensitive(True)
            self._device_row.set_sensitive(False)
            self._source_row.row.set_sensitive(False)
        else:
            self._device_row.set_sensitive(True)
            self._source_row.row.set_sensitive(True)

    def set_cancel_enabled(self, enabled: bool) -> None:
        """Toggle the Cancel action (disabled while cancelling)."""
        self._cancel_btn.set_sensitive(enabled)

    def set_refresh_enabled(self, enabled: bool) -> None:
        """Toggle the device refresh action."""
        self._refresh_btn.set_sensitive(enabled)

    def set_scan_enabled(self, enabled: bool) -> None:
        """Toggle the primary Scan action."""
        self._scan_btn.set_sensitive(enabled)
