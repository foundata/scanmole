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

    assert form._size_pref_dropdown.get_sensitive() is True  # Automatic
    form.apply_settings({"page_size": "a4"})
    assert form._size_pref_dropdown.get_sensitive() is False
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
    preview = form._name_preview.get_text()
    assert "scan_001.pdf" in preview  # the {NNN} counter, zero-padded

    form._name_entry.set_text("receipt_{device}")
    assert form.persisted_values()["filename_template"] == "receipt_{device}.pdf"
    assert "epsonds" in form._name_preview.get_text()


def test_running_state_toggles_the_form() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})

    form.set_running(True)
    assert form._scan_row.get_visible() is False
    assert form._cancel_row.get_visible() is True
    assert form.scanner_group.get_sensitive() is True  # hosts Cancel
    assert form._device_row.get_sensitive() is False
    assert form._source_row.row.get_sensitive() is False
    assert form.document_group.get_sensitive() is False

    form.set_running(False)
    assert form._scan_row.get_visible() is True
    assert form._cancel_row.get_visible() is False
    assert form._device_row.get_sensitive() is True
    assert form.document_group.get_sensitive() is True


def test_source_changes_carry_the_manual_context() -> None:
    events = Events()
    form = _form(events)
    form.apply_settings({})
    events.calls.clear()

    form._source_row.select("adf")  # what a user click goes through
    assert ("source", (True,)) in events.calls

    events.calls.clear()
    form.select_source("flatbed")  # a flow reconciliation
    assert ("source", (False,)) in events.calls
    assert form.source_value() == "flatbed"


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
    _click_choice(form._mode_row, 3)  # and clicks back onto faint
    assert form.mode_value() == "gray"  # reverted, never adopted
    assert ("blocked", ("lineart-auto", "plain 1-bit only")) in events.calls

    form.set_source_availability({"adf-duplex": "no duplex"})
    assert form.selection_blocked_reason() == "no duplex"  # saved source blocked


def test_folder_updates_the_button_label() -> None:
    events = Events()
    form = _form(events)
    home = str(Path.home())

    form.set_folder(f"{home}/Scans")
    assert form.folder() == f"{home}/Scans"
