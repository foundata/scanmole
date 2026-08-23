"""Regression tests for the ScanForm component (PyGObject plus display)."""

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


class Events:
    """Records every orchestration callback the form emits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def cb(self, name: str) -> Any:
        def record(*args: object) -> None:
            self.calls.append((name, args))

        return record

    def names(self) -> list[str]:
        return [name for name, _args in self.calls]


def _form(
    events: Events,
    device: str | None = "sane:0",
    effective: int | None = None,
) -> Any:
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    from scanmole_gui.form import ScanForm

    return ScanForm(
        on_device_selected=events.cb("device"),
        on_source_changed=events.cb("source"),
        on_refresh=events.cb("refresh"),
        on_scan=events.cb("scan"),
        on_cancel=events.cb("cancel"),
        on_pick_folder=events.cb("pick_folder"),
        on_more_languages=events.cb("more_languages"),
        on_choice_blocked=events.cb("blocked"),
        on_hardware_button_selected=events.cb("button_pref"),
        on_insert_to_scan=events.cb("insert_pref"),
        on_open_settings=events.cb("open_settings"),
        on_preview_stale=events.cb("preview"),
        device_for_preview=lambda: device,
        effective_resolution=lambda _dpi: effective,
    )


def _click_choice(row: Any, index: int) -> None:
    """Activate a ChoiceRow item like a user click, on either backend."""
    if row._toggles is not None:
        row._toggles.set_active(index)
    else:
        row._combo.set_selected(index)


SETTINGS: dict[str, object] = {
    "source": "adf",
    "mode": "gray",
    "resolution": "250",
    "page_size": "a5",
    "auto_size_preference": "north-american",
    "ocr": False,
    "deskew": False,
    "lang": "eng",
    "skip_blanks": False,
    "filename_template": "batch_{N}.pdf",
    "folder": "/tmp/scans",
}


def test_apply_settings_round_trips_into_persisted_values() -> None:
    events = Events()
    form = _form(events)

    form.apply_settings(dict(SETTINGS))

    persisted = form.persisted_values()
    for key, value in SETTINGS.items():
        if key == "source":
            continue  # the preferred source is the window's, not the form's
        assert persisted[key] == value, key
    assert form.source_value() == "adf"


def test_defaults_apply_for_missing_and_broken_settings() -> None:
    events = Events()
    form = _form(events)

    form.apply_settings({"resolution": "not-a-number"})

    persisted = form.persisted_values()
    assert persisted["mode"] == "lineart"
    assert persisted["resolution"] == "300"  # broken value falls back
    assert persisted["page_size"] == "auto"
    assert persisted["auto_size_preference"] == "iso"
    assert persisted["ocr"] is True and persisted["deskew"] is True
    assert persisted["lang"] == "deu+eng"
    assert persisted["skip_blanks"] is True
    # The empty template persists as the default with .pdf ensured.
    from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE

    assert persisted["filename_template"] == DEFAULT_OUTPUT_TEMPLATE
    assert form.source_value() == "adf-duplex"


def test_scan_request_matches_the_form_values() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings(dict(SETTINGS))

    request = form.scan_request("sane:0", Path("/tmp/out"))

    assert request.device == "sane:0"
    assert request.source == "adf"
    assert request.mode == "gray"
    assert request.resolution == 250
    assert request.page_size == "a5"
    assert request.auto_size_preference == "north-american"
    assert request.ocr is False
    assert request.lang == "eng"
    assert request.deskew is False
    assert request.drop_blanks is False
    assert request.output == "/tmp/out/batch_{N}.pdf"


def test_resolution_typing_stepping_clamping_and_presets() -> None:
    events = Events()
    form = _form(events)

    form._res_entry.set_text("240")
    form._on_resolution_commit()
    assert form.resolution() == 240

    form._step_resolution(10)
    assert form.resolution() == 250
    assert [c.get_active() for c, _p in form._res_chips] == [False, True, False, False]

    form._res_entry.set_text("9")
    form._on_resolution_commit()
    assert form.resolution() == 50  # clamped to the minimum

    form._res_entry.set_text("9999")
    form._on_resolution_commit()
    assert form.resolution() == 1200  # clamped to the maximum

    form._res_entry.set_text("junk")
    form._on_resolution_commit()
    assert form.resolution() == 1200  # invalid input reverts

    chip_600 = form._res_chips[3][0]
    chip_600.set_active(True)
    assert form.resolution() == 600  # preset chip fills the entry


def test_effective_resolution_hint_renders_in_the_subtitle() -> None:
    events = Events()
    form = _form(events, effective=150)
    form.apply_settings({"resolution": "300"})

    subtitle = form._res_row.get_subtitle()
    assert "300" in subtitle and "150" in subtitle  # requested and effective

    plain = _form(Events(), effective=None)
    plain.apply_settings({"resolution": "300"})
    assert "150" not in plain._res_row.get_subtitle()


def test_page_size_gates_the_family_preference() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    assert form._size_pref_row.get_sensitive() is True  # Automatic
    form.apply_settings({"page_size": "a4"})
    assert form._size_pref_row.get_sensitive() is False
    # The disabled dropdown keeps its value.
    assert form.persisted_values()["auto_size_preference"] == "iso"


def test_ocr_gates_the_language_row_and_custom_codes_join_the_list() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    assert form._lang_row.get_sensitive() is True
    form._ocr_row.set_active(False)
    assert form._lang_row.get_sensitive() is False

    form.select_language("fra+ita")
    assert form.selected_language() == "fra+ita"
    assert ("fra+ita", "fra+ita") in form._languages
    form.select_language("deu")  # built-ins still selectable
    assert form.selected_language() == "deu"


def test_filename_defaulting_and_preview() -> None:
    events = Events()
    form = _form(events, device="epsonds:net:10.0.0.2")
    form.apply_settings({})

    # Empty entry means the default template, with .pdf ensured.
    from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE

    assert form.persisted_values()["filename_template"] == DEFAULT_OUTPUT_TEMPLATE
    assert form.preview_template() == DEFAULT_OUTPUT_TEMPLATE
    # Which name is free depends on the folder, so the form asks rather
    # than answering: it renders whatever comes back.
    assert ("preview", ()) in events.calls

    form._name_entry.set_text("receipt_{device}")
    assert form.persisted_values()["filename_template"] == "receipt_{device}.pdf"
    assert form.preview_template() == "receipt_{device}.pdf"


def test_running_state_toggles_the_form() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    form.set_running(True)
    assert form._scan_row.get_visible() is False
    assert form._cancel_row.get_visible() is True
    # The Scan card hosts Cancel, so it stays sensitive while every one
    # of its settings rows locks individually.
    assert form.scan_group.get_sensitive() is True
    assert [row.get_sensitive() for row in form._scan_setting_rows] == [False] * len(
        form._scan_setting_rows
    )
    assert form.processing_group.get_sensitive() is False

    form.set_running(False)
    assert form._scan_row.get_visible() is True
    assert form._cancel_row.get_visible() is False
    assert [row.get_sensitive() for row in form._scan_setting_rows] == [True] * len(
        form._scan_setting_rows
    )
    assert form.processing_group.get_sensitive() is True


def test_source_changes_carry_the_manual_context() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})
    events.calls.clear()

    # The default is the duplex feeder, so a click onto the flatbed is a
    # real change on the paper-path row.
    form._path_row.select("flatbed")  # what a user click goes through
    assert ("source", (True,)) in events.calls
    assert form.source_value() == "flatbed"

    events.calls.clear()
    form.select_source("adf-back")  # a flow reconciliation
    assert ("source", (False,)) in events.calls
    assert form.source_value() == "adf-back"
    # One report per composite update, not one per row that moved.
    assert events.names().count("source") == 1


def test_availability_passes_through_with_the_blocked_callback() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    # The saved "B/W (faint)" choice turns out blocked: it stays selected
    # and visible with its reason so the window can gate Start.
    form._mode_row.select("lineart-auto")
    form.set_mode_availability({"lineart-auto": "plain 1-bit only"})
    assert form.selection_blocked_reason() == "plain 1-bit only"

    _click_choice(form._mode_row, 1)  # the user moves to Gray
    assert form.mode_value() == "gray"
    assert form.selection_blocked_reason() is None
    if form._mode_row._toggles is not None:
        # The abandoned blocked choice lost its active-toggle exemption:
        # it renders disabled, so there is nothing to click back onto.
        assert form._mode_row._toggles.get_toggle(3).get_enabled() is False
    else:
        _click_choice(form._mode_row, 3)  # clicking back onto faint reverts
        assert form.mode_value() == "gray"  # never adopted
        assert ("blocked", ("lineart-auto", "plain 1-bit only")) in events.calls

    form.set_source_availability({"adf-duplex": "no duplex"})
    assert form.selection_blocked_reason() == "no duplex"  # saved source blocked


def test_folder_updates_the_button_label() -> None:
    events = Events()
    form = _form(events)
    home = str(Path.home())

    form.set_folder(f"{home}/Scans")
    assert form.folder() == f"{home}/Scans"


def test_primary_scan_uses_the_persisted_sheet_flow() -> None:
    events = Events()
    form = _form(events)

    form._scan_btn.emit("clicked")
    form._collect_row.set_active(True)
    form._scan_btn.emit("clicked")

    assert events.calls == [("scan", ("stack",)), ("scan", ("collect",))]


def test_menu_overrides_are_one_shot_and_leave_the_form_alone() -> None:
    from scanmole_gui.form import FLOW_ACTIONS

    events = Events()
    form = _form(events)
    popover = form._scan_btn.get_popover()
    box = popover.get_child()
    children = []
    child = box.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    # A dim caption marks every action below as one-shot, GNOME-style.
    assert children[0].__class__.__name__ == "Label"
    assert children[0].get_text() == "For this scan only"
    buttons = [c for c in children if c.__class__.__name__ == "Button"]
    assert len(buttons) == len(FLOW_ACTIONS)

    for button in buttons:
        button.emit("clicked")

    flows = [args[0] for name, args in events.calls if name == "scan"]
    assert flows == ["single", "stack", "collect"]
    # The overrides never touched the persisted choice.
    assert form._collect_row.get_active() is False
    assert form.sheet_flow_value() == "stack"


def test_collect_toggle_round_trips_through_settings() -> None:
    events = Events()
    form = _form(events)

    form.apply_settings({"wait_for_more_sheets": True})
    assert form.persisted_values()["wait_for_more_sheets"] is True
    assert form.sheet_flow_value() == "collect"

    form.apply_settings({})  # tolerant default
    assert form.persisted_values()["wait_for_more_sheets"] is False


def test_scan_request_takes_the_flow_from_the_trigger_not_the_widgets() -> None:
    events = Events()
    form = _form(events)
    form._collect_row.set_active(True)  # the persisted choice says collect

    request = form.scan_request("sane:0", Path("/tmp/out"), sheet_flow="single")

    assert request.sheet_flow == "single"
    assert form._collect_row.get_active() is True


def test_stack_switch_selects_single_on_a_feeder() -> None:
    events = Events()
    form = _form(events)
    form._apply_source("adf")

    assert form.sheet_flow_value() == "stack"  # on by default

    form._stack_row.set_active(False)
    assert form.sheet_flow_value() == "single"

    form._collect_row.set_active(True)
    assert form.sheet_flow_value() == "collect"  # collect wins over both


def test_stack_switch_is_gated_and_ignored_on_the_flatbed() -> None:
    events = Events()
    form = _form(events)
    form._apply_source("adf")
    form._stack_row.set_active(False)
    assert form._stack_row.get_sensitive() is True

    form._apply_source("flatbed")

    # A flatbed has no loaded stack: the switch grays out and its off
    # state never turns the scan into single.
    assert form._stack_row.get_sensitive() is False
    assert form.sheet_flow_value() == "stack"

    form._apply_source("adf-duplex")
    assert form._stack_row.get_sensitive() is True
    assert form.sheet_flow_value() == "single"  # the off state was kept


def test_stack_switch_round_trips_through_settings() -> None:
    events = Events()
    form = _form(events)

    form.apply_settings({"scan_loaded_stack": False, "source": "adf"})
    assert form.persisted_values()["scan_loaded_stack"] is False

    form.apply_settings({})  # tolerant default: on
    assert form.persisted_values()["scan_loaded_stack"] is True


def test_primary_scan_uses_the_single_flow_when_stack_is_off() -> None:
    events = Events()
    form = _form(events)
    form._apply_source("adf")
    form._stack_row.set_active(False)
    events.calls.clear()

    form._scan_btn.emit("clicked")

    assert events.calls == [("scan", ("single",))]


def test_scanner_trigger_rows_fire_and_round_trip() -> None:
    from scanmole_gui.form import HARDWARE_BUTTON_ACTIONS
    from scanmole_gui.widgets import combo_select

    events = Events()
    form = _form(events)
    assert events.calls == []  # construction fires no preference callbacks

    combo_select(form._button_row, HARDWARE_BUTTON_ACTIONS, "single")
    form._insert_row.set_active(True)
    assert ("button_pref", ("single",)) in events.calls
    assert ("insert_pref", (True,)) in events.calls

    persisted = form.persisted_values()
    assert persisted["hardware_button"] == "single"
    assert persisted["insert_to_scan"] is True

    form.apply_settings({"hardware_button": "bogus"})  # tolerant fallback
    assert form.persisted_values()["hardware_button"] == "same"
    assert form.persisted_values()["insert_to_scan"] is False


def _find_row(widget: Any, title: str) -> Any:
    """The row carrying ``title`` anywhere under ``widget``."""
    if widget.__class__.__name__ == "ActionRow" and widget.get_title() == title:
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_row(child, title)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _labels(widget: Any) -> list[Any]:
    """Every label in a widget subtree, in order."""
    from gi.repository import Gtk

    found = []
    if isinstance(widget, Gtk.Label):
        found.append(widget)
    child = widget.get_first_child()
    while child is not None:
        found.extend(_labels(child))
        child = child.get_next_sibling()
    return found


def _group_titles(group: Any) -> list[str]:
    titles: list[str] = []
    child = group.get_first_child()
    while child is not None:  # walk into the group's list box
        titles.extend(_row_titles(child))
        child = child.get_next_sibling()
    return titles


def test_the_scan_group_puts_the_flow_rows_above_the_action() -> None:
    events = Events()
    form = _form(events)

    titles = _group_titles(form.scan_group)
    assert titles[-2:] == ["Combine scans", "Auto-start when paper is inserted"]
    # Both qualify the button they sit above, and both lock with the
    # rest of the card's settings while a scan runs.
    assert form._scan_row.get_parent() is not None
    assert form._collect_row in form._scan_setting_rows
    assert form._insert_row in form._scan_setting_rows
    assert not hasattr(form, "behaviour_group")


def test_the_resolution_presets_carry_their_recommendation() -> None:
    events = Events()
    form = _form(events)

    hints = [
        child.get_label()
        for child in _labels(form._chips_row)
        if child.get_label() and "dim-label" in child.get_css_classes()
    ]
    assert hints == [
        "Recommended for reasonably small files: 300 dpi for B/W, 200 for Gray "
        "and Color"
    ]


def test_the_one_shot_menu_names_the_switches_it_stands_in_for() -> None:
    # The menu action and the switch are the same flow, so they carry the
    # same word; the caption above them says it applies to one run.
    from scanmole_gui.form import FLOW_ACTIONS

    assert [label for label, _value in FLOW_ACTIONS] == [
        "Scan one sheet",
        "Scan loaded stack",
        "Combine scans",
    ]
    assert dict(FLOW_ACTIONS)["Combine scans"] == "collect"


@pytest.mark.parametrize(
    ("missing", "ocr", "mode", "shown"),
    [
        pytest.param(True, True, "lineart", True, id="applies"),
        pytest.param(True, True, "lineart-auto", True, id="faint-is-1-bit-too"),
        pytest.param(True, False, "lineart", False, id="no-ocr-no-pass"),
        pytest.param(True, True, "gray", False, id="gray-pages"),
        pytest.param(True, True, "color", False, id="color-pages"),
        pytest.param(False, True, "lineart", False, id="already-installed"),
    ],
)
def test_the_encoder_hint_appears_only_where_it_applies(
    monkeypatch: pytest.MonkeyPatch, missing: bool, ocr: bool, mode: str, shown: bool
) -> None:
    # The optional encoder only shrinks 1-bit pages, and only inside the
    # optimization pass OCR runs, so nobody who cannot benefit sees it.
    monkeypatch.setattr("scanmole_gui.form.jbig2_missing", lambda: missing)
    events = Events()
    form = _form(events)
    form.apply_settings({})

    form._ocr_row.set_active(ocr)
    form._mode_row.select(mode)

    assert form.jbig2_hint_applies() is shown
    assert form._jbig2_row.get_visible() is shown


def test_the_advanced_group_opens_the_settings_dialog() -> None:
    events = Events()
    form = _form(events)

    assert _group_titles(form.advanced_group) == ["Advanced settings"]
    row = _find_row(form.advanced_group, "Advanced settings")
    assert row is not None
    # The way in is named after the menu entry it duplicates.
    assert row.get_activatable_widget().get_child().get_label() == "Settings"
    assert row.get_subtitle() == "More control over scans and the application"
    row.get_activatable_widget().emit("clicked")

    assert events.names() == ["open_settings"]


def test_the_rarer_rows_wait_in_the_borrowed_settings_groups() -> None:
    # They are still the form's rows, read for the request and written
    # back from the saved settings; only where they are shown changed.
    events = Events()
    form = _form(events)

    assert _group_titles(form.settings_scan_group) == [
        "Page size",
        "Preferred paper sizes",
    ]
    assert _group_titles(form.settings_processing_group) == [
        "Deskew",
        "Create <a href='https://en.wikipedia.org/wiki/PDF/A'>PDF/A</a> files",
    ]
    assert _group_titles(form.settings_behaviour_group) == [
        "Scan all pages in feeder",
        "Hardware scan button",
    ]
    # None of them is on the page: the dialog puts them up while open.
    assert form.settings_scan_group.get_parent() is None
    assert form.settings_processing_group.get_parent() is None
    assert form.settings_behaviour_group.get_parent() is None

    form.apply_settings({"deskew": False, "scan_loaded_stack": False})
    values = form.persisted_values()
    assert values["deskew"] is False
    assert values["scan_loaded_stack"] is False
    assert form.scan_request("dev", Path("out")).deskew is False


def _row_titles(widget: Any) -> list[str]:
    titles: list[str] = []
    if widget.__class__.__name__ in ("SwitchRow", "ComboRow", "ActionRow"):
        title = widget.get_title()
        if title:
            return [title]
    child = widget.get_first_child()
    while child is not None:
        titles.extend(_row_titles(child))
        child = child.get_next_sibling()
    return titles


def test_processing_group_orders_page_steps_before_the_ocr_chain() -> None:
    events = Events()
    form = _form(events)

    # The page-level step first, then the OCR chain. Deskew and the
    # archival output moved to the settings dialog.
    assert _group_titles(form.processing_group) == [
        "Skip blank pages",
        "OCR (Optical Character Recognition)",
        "OCR Language",
    ]


def test_the_preview_row_shows_the_bare_next_file_name() -> None:
    events = Events()
    form = _form(events, device="epsonds:net:10.0.0.2")
    form.apply_settings({})

    # The row carries the wording; the value is the file name alone, so
    # it reads as a monospace value next to its label.
    form.set_preview("2026-08-23_scan_003.pdf")
    assert form._name_preview.get_text() == "2026-08-23_scan_003.pdf"
    assert "monospace" in form._name_preview.get_css_classes()
    row = _find_row(form.output_group, "Preview")
    assert row is not None
    assert row.get_subtitle() == "Expected output filename"


def test_archival_toggle_round_trips_and_follows_ocr() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    assert form.persisted_values()["pdfa"] is True  # the CLI's own default
    assert form._pdfa_row.get_sensitive() is True

    form._ocr_row.set_active(False)  # PDF/A comes from the OCR stage
    assert form._pdfa_row.get_sensitive() is False
    assert form._lang_row.get_sensitive() is False

    form.apply_settings({"pdfa": False})
    assert form.persisted_values()["pdfa"] is False
    assert form.scan_request("sane:0", Path("/tmp")).pdfa is False


def test_the_two_source_rows_compose_the_engine_value() -> None:
    events = Events()
    form = _form(events)

    for source, path, side in (
        ("adf", "feeder", "front"),
        ("adf-back", "feeder", "back"),
        ("adf-duplex", "feeder", "both"),
        ("flatbed", "flatbed", None),
    ):
        form._apply_source(source)
        assert form._path_row.value() == path, source
        if side is not None:
            assert form._sides_row.value() == side, source
        assert form.source_value() == source

    # The flatbed has no side to choose, so the row is inert and its
    # value cannot leak into the composed source.
    form._apply_source("flatbed")
    assert form._sides_row.row.get_sensitive() is False
    form._sides_row.select("back")
    assert form.source_value() == "flatbed"


def test_source_availability_splits_across_the_two_rows() -> None:
    events = Events()
    form = _form(events)

    # The iX100: one simplex feeder, nothing else. The feeder itself
    # stays available because one of its sides is.
    form.set_source_availability(
        {
            "flatbed": "no flatbed",
            "adf-duplex": "no duplex",
            "adf-back": "no back side",
        }
    )
    assert form._path_row._blocked == {"flatbed": "no flatbed"}
    assert form._sides_row._blocked == {"both": "no duplex", "back": "no back side"}

    # A flatbed-only device: every feeder side is out, so the feeder is too.
    form.set_source_availability(
        {"adf": "no feeder", "adf-duplex": "no feeder", "adf-back": "no feeder"}
    )
    assert form._path_row._blocked == {"feeder": "no feeder"}
    assert set(form._sides_row._blocked) == {"front", "back", "both"}


def test_a_blocked_saved_side_keeps_start_disabled() -> None:
    # The saved choice is applied before the probe lands, exactly as at
    # startup; the arriving block must not change it, only explain it.
    events = Events()
    form = _form(events)
    form._apply_source("adf-duplex")

    form.set_source_availability({"adf-duplex": "no duplex"})

    assert form.source_value() == "adf-duplex"  # never silently changed
    assert form.selection_blocked_reason() == "no duplex"


def test_a_blocked_side_is_ignored_while_the_flatbed_is_selected() -> None:
    events = Events()
    form = _form(events)
    form.set_source_availability({"adf-duplex": "no duplex"})
    form._apply_source("adf-duplex")
    assert form.selection_blocked_reason() == "no duplex"

    form._path_row.select("flatbed")

    # The sides row is inert on the flatbed, so its blocked value must
    # not keep Start disabled.
    assert form.selection_blocked_reason() is None


def test_the_paper_family_defaults_to_the_locale_on_a_first_start() -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from scanmole_gui import form as form_module

    events = Events()
    form = _form(events)
    original = Gtk.PaperSize.get_default

    def with_locale_paper(name: str, settings: dict[str, object]) -> str:
        form_module._locale_paper_family.cache_clear()
        Gtk.PaperSize.get_default = staticmethod(lambda value=name: value)
        try:
            form.apply_settings(settings)
        finally:
            Gtk.PaperSize.get_default = original
        return str(form.persisted_values()["auto_size_preference"])

    # No saved value: the desktop's paper convention decides, and that is
    # LC_PAPER rather than the interface language, so an English desktop in
    # Germany still gets ISO.
    for paper_name, expected in (
        ("iso_a4", "iso"),
        ("na_letter", "north-american"),
        ("na_legal", "north-american"),
        ("jis_b4", "iso"),  # neither family; ISO is the right A4/Letter tie
        ("", "iso"),
    ):
        assert with_locale_paper(paper_name, {}) == expected, paper_name

    # A saved choice always wins over the locale.
    assert with_locale_paper("iso_a4", {"auto_size_preference": "north-american"}) == (
        "north-american"
    )


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({}, "same"),  # a profile that never chose one
        ({"hardware_button": "bogus"}, "same"),  # unrecognized
        ({"hardware_button": ""}, "same"),  # empty is not a choice
        ({"hardware_button": "off"}, "off"),  # an explicit choice is kept
        ({"hardware_button": "same"}, "same"),
        ({"hardware_button": "single"}, "single"),
        ({"hardware_button": "collect"}, "collect"),
    ],
)
def test_the_button_mapping_round_trips_and_defaults_to_same(
    saved: dict[str, object], expected: str
) -> None:
    # The scanner's own button follows the Scan button unless the user
    # turned it off, so only an explicitly saved "off" stays off.
    from scanmole_gui.form import hardware_button_value

    events = Events()
    form = _form(events)
    form.apply_settings(saved)

    assert form.persisted_values()["hardware_button"] == expected
    # The form and the window's runtime reading agree by construction.
    assert hardware_button_value(saved) == expected
