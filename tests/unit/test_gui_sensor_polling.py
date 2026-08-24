"""Tests for MainWindow's sensor-trigger adapter (no hardware).

The device-lifecycle controller consumes sensor edges and hands the
window one typed request; the window resolves ``same`` against the
form's current sheet flow, re-checks the authoritative Start predicate
and launches. Poll arming, edge consumption and the observation policy
are pinned in ``test_gui_deviceflow.py`` and ``test_sensorwatch.py``.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


def _trigger(mapping: str, reason: str = "hardware button") -> Any:
    from scanmole_gui.deviceflow import SensorTrigger

    return SensorTrigger(mapping=mapping, reason=reason)


def _trigger_window(*, allowed: bool = True, form: Any = None) -> Any:
    from scanmole_gui.app import MainWindow

    class Window:
        _on_sensor_trigger = MainWindow._on_sensor_trigger
        _sensor_trigger_allowed = MainWindow._sensor_trigger_allowed
        _window_suspended = MainWindow._window_suspended

        def __init__(self) -> None:
            self._released = False
            self.allowed = allowed
            self.started: list[str] = []
            self.logs: list[str] = []

            class Form:
                @staticmethod
                def sheet_flow_value() -> str:
                    return "collect"  # the persisted choice: collect is on

            self._form = form if form is not None else Form()

        def get_visible(self) -> bool:
            return True

        def _scan_allowed(self) -> bool:
            return self.allowed

        def _on_scan_clicked(self, flow: str = "stack") -> None:
            self.started.append(flow)

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

    return Window()


@_NEEDS_GI
@pytest.mark.parametrize(
    ("mapping", "expected"),
    [("same", "collect"), ("single", "single"), ("collect", "collect")],
)
def test_the_mapping_resolves_against_the_form(mapping: str, expected: str) -> None:
    window = _trigger_window()

    window._on_sensor_trigger(_trigger(mapping))

    assert window.started == [expected]
    assert any("hardware button: starting a scan" in line for line in window.logs)


@_NEEDS_GI
def test_an_insertion_uses_the_persisted_flow() -> None:
    window = _trigger_window()

    window._on_sensor_trigger(_trigger("same", reason="paper inserted"))

    assert window.started == ["collect"]
    assert any("paper inserted: starting a scan" in line for line in window.logs)


@_NEEDS_GI
def test_a_blocked_start_declines_the_trigger_silently() -> None:
    # The authoritative predicate decides again at delivery; a declined
    # trigger was already consumed by the controller and never queues.
    window = _trigger_window(allowed=False)

    window._on_sensor_trigger(_trigger("same"))

    assert window.started == []
    assert window.logs == []


@_NEEDS_GI
def test_a_hidden_or_released_window_declines_the_trigger() -> None:
    hidden = _trigger_window()
    hidden.is_suspended = lambda: True
    hidden._on_sensor_trigger(_trigger("same"))
    assert hidden.started == []

    released = _trigger_window()
    released._released = True
    released._on_sensor_trigger(_trigger("same"))
    assert released.started == []


@_NEEDS_GI
@pytest.mark.parametrize("visible", [True, False])
@pytest.mark.parametrize("suspended", [True, False])
def test_the_context_reports_visibility_and_suspension_independently(
    visible: bool, suspended: bool
) -> None:
    # get_visible() and is_suspended() are different questions (an
    # ordinarily hidden window is not compositor-suspended, and older
    # GTK never reports suspension at all); the context must carry each
    # answer as it is, never derive one from the other.
    from scanmole_gui.app import MainWindow

    class Window:
        _device_context = MainWindow._device_context
        _sensor_prefs = MainWindow._sensor_prefs
        _window_suspended = MainWindow._window_suspended

        def __init__(self) -> None:
            self._settings: dict[str, object] = {}

            class Form:
                @staticmethod
                def source_value() -> str:
                    return "adf-duplex"

            self._form = Form()

        def get_visible(self) -> bool:
            return visible

        def is_suspended(self) -> bool:
            return suspended

        def _selected_device(self) -> str | None:
            return "test:0"

        def _scan_allowed(self) -> bool:
            return True

    context = Window()._device_context()  # type: ignore[misc]

    assert context.visible is visible
    assert context.suspended is suspended


@_NEEDS_GI
@pytest.mark.parametrize(
    ("visible", "suspended", "allowed"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),  # ordinarily hidden, not suspended
        (False, True, False),
    ],
)
def test_the_trigger_predicate_rechecks_visibility_and_suspension(
    visible: bool, suspended: bool, allowed: bool
) -> None:
    window = _trigger_window()
    window.get_visible = lambda: visible
    window.is_suspended = lambda: suspended

    window._on_sensor_trigger(_trigger("same"))

    assert bool(window.started) is allowed


def _prefs_window(settings: dict[str, object]) -> Any:
    """A window stub carrying only what the preference reader needs."""
    from scanmole_gui.app import MainWindow

    class Window:
        _sensor_prefs = MainWindow._sensor_prefs

        def __init__(self) -> None:
            self._settings = settings

    return Window()


@_NEEDS_GI
def test_the_form_and_the_window_read_the_same_button_mapping() -> None:
    # One fallback, applied wherever the value is interpreted: the row the
    # user sees and the runtime trigger preference cannot disagree.
    from scanmole_gui.form import hardware_button_value

    for saved, expected in (
        ({}, "same"),
        ({"hardware_button": "bogus"}, "same"),
        ({"hardware_button": "off"}, "off"),
        ({"hardware_button": "single"}, "single"),
        ({"hardware_button": "collect"}, "collect"),
    ):
        window = _prefs_window(dict(saved))
        assert window._sensor_prefs()[0] == expected
        assert hardware_button_value(saved) == expected


@_NEEDS_GI
@pytest.mark.parametrize(
    ("collect", "stack", "expected"),
    [(True, True, "collect"), (False, True, "stack"), (False, False, "single")],
)
def test_the_button_starts_the_configured_sheet_mode_without_saving_one(
    collect: bool, stack: bool, expected: str
) -> None:
    # "Same as Scan" means exactly that: it reads what a Scan click would
    # do and leaves the persisted choice alone, so a press is never a
    # hidden way to change the form.
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    from scanmole_gui.form import ScanForm

    noop: Any = lambda *_a, **_k: None  # noqa: E731
    form = ScanForm(
        on_device_selected=noop,
        on_source_changed=noop,
        on_refresh=noop,
        on_scan=noop,
        on_cancel=noop,
        on_pick_folder=noop,
        on_more_languages=noop,
        on_choice_blocked=noop,
        on_hardware_button_selected=noop,
        on_insert_to_scan=noop,
        on_open_settings=noop,
        on_preview_stale=noop,
        device_for_preview=lambda: "test:0",
        effective_resolution=lambda _dpi: None,
    )
    form.apply_settings(
        {"wait_for_more_sheets": collect, "scan_loaded_stack": stack, "source": "adf"}
    )
    before = form.persisted_values()

    window = _trigger_window(form=form)
    window._on_sensor_trigger(_trigger("same"))

    assert window.started == [expected]
    assert form.persisted_values() == before  # nothing was written back
