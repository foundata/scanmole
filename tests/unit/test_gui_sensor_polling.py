"""Tests for MainWindow's sensor polling and trigger mapping (no hardware).

The window decides when polling is worth doing at all, maps a trigger to
a flow and drops anything stale or blocked. The observation policy it
feeds from is pinned in ``test_sensorwatch.py``.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from scanmole.sensors import SensorSnapshot
from scanmole_gui.sensorwatch import SensorArbiter

_IDLE = SensorSnapshot(scan=False, page_loaded=False)
_BUTTON = SensorSnapshot(scan=True, page_loaded=False)
_PAPER = SensorSnapshot(scan=False, page_loaded=True)
_BOTH = SensorSnapshot(scan=True, page_loaded=True)

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)


def _duck_window(
    *,
    mapping: str = "off",
    insert: bool = False,
    allowed: bool = True,
    form: Any = None,
) -> Any:
    from scanmole_gui.app import MainWindow

    class Window:
        _observe_sensors = MainWindow._observe_sensors
        _sensor_prefs = MainWindow._sensor_prefs
        _sensor_trigger_allowed = MainWindow._sensor_trigger_allowed
        _window_suspended = MainWindow._window_suspended
        _trigger_sensor_scan = MainWindow._trigger_sensor_scan
        _on_sensor_poll_done = MainWindow._on_sensor_poll_done

        def __init__(self) -> None:
            self._settings = {"hardware_button": mapping, "insert_to_scan": insert}
            self._sensor_arbiter = SensorArbiter()
            self._sensor_poll_busy = False
            self._released = False
            self._searching = False
            self._runner = None
            self._advisory = type("A", (), {"generation": 0})()
            self.allowed = allowed
            self.started: list[str] = []
            self.logs: list[str] = []
            self.refreshes = 0
            self.scheduled = 0

            class Form:
                @staticmethod
                def sheet_flow_value() -> str:
                    return "collect"  # the persisted choice: collect is on

            self._form = form if form is not None else Form()

        def _scan_allowed(self) -> bool:
            return self.allowed

        def _on_scan_clicked(self, flow: str = "stack") -> None:
            self.started.append(flow)

        def _append_log(self, text: str) -> None:
            self.logs.append(text)

        def _refresh_devices(self) -> None:
            self.refreshes += 1

        def _schedule_sensor_poll(self) -> None:
            self.scheduled += 1

    return Window()


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ("same", ["collect"]),
        ("single", ["single"]),
        ("collect", ["collect"]),
        ("off", []),
    ],
)
def test_button_mapping_selects_the_flow(mapping: str, expected: list[str]) -> None:
    window = _duck_window(mapping=mapping)
    window._observe_sensors(_IDLE)  # baseline

    window._observe_sensors(_BUTTON)

    assert window.started == expected


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_insertion_starts_the_persisted_flow_only_when_enabled() -> None:
    enabled = _duck_window(insert=True)
    enabled._observe_sensors(_IDLE)
    enabled._observe_sensors(_PAPER)
    assert enabled.started == ["collect"]

    disabled = _duck_window(insert=False)
    disabled._observe_sensors(_IDLE)
    disabled._observe_sensors(_PAPER)
    assert disabled.started == []


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_button_beats_an_insertion_in_the_same_observation() -> None:
    window = _duck_window(mapping="single", insert=True)
    window._observe_sensors(_IDLE)

    window._observe_sensors(_BOTH)

    assert window.started == ["single"]  # the explicit mapping won


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_blocked_trigger_is_consumed_and_never_queued() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)
    window.allowed = False

    window._observe_sensors(_BUTTON)
    assert window.started == []

    # Once Start is allowed again, the consumed press must not replay;
    # only a genuinely new edge triggers.
    window.allowed = True
    window._observe_sensors(_BUTTON)  # still latched: no new edge
    assert window.started == []
    window._observe_sensors(_IDLE)
    window._observe_sensors(_BUTTON)
    assert window.started == ["collect"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_an_open_failure_requests_discovery_and_logs_once() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)

    window._on_sensor_poll_done(None, 0)
    window._on_sensor_poll_done(None, 0)

    assert window.refreshes == 2  # discovery takes over
    outage_lines = [line for line in window.logs if "sensor" in line]
    assert len(outage_lines) == 1  # one line per outage, no error storm
    assert window.scheduled == 0  # polling stopped until discovery decides


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_stale_or_released_poll_result_is_dropped() -> None:
    window = _duck_window(mapping="same")
    window._observe_sensors(_IDLE)

    window._advisory.generation = 7  # cancelled underneath
    window._on_sensor_poll_done(_BUTTON, 0)
    assert window.started == []
    assert window.scheduled == 0

    window._advisory.generation = 0
    window._released = True
    window._on_sensor_poll_done(_BUTTON, 0)
    assert window.started == []


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_only_flow_accepted_probe_evidence_feeds_the_arbiter() -> None:
    # A live capability probe read the device and consumed any latch, but
    # only the flow knows whether that read still describes the current
    # selection. A result it rejects (another device, a source the user
    # has left) must not arm anything, and a cached snapshot never
    # reaches this path at all.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.probing import CapabilityUpdate, ProbeRequest

    order: list[str] = []
    outcome: list[CapabilityUpdate] = []

    class Window:
        _on_probe_done = MainWindow._on_probe_done

        def __init__(self) -> None:
            self._released = False
            self._advisory = type("A", (), {"generation": 0})()
            self.observed: list[object] = []

            class Flow:
                @staticmethod
                def probe_completed(*args: object) -> object:
                    order.append("flow")
                    return outcome[0]

            self._flow = Flow()

        def _selected_device(self) -> str:
            return "sane:0"

        class _Form:
            @staticmethod
            def source_value() -> str:
                return "adf"

        _form = _Form()

        def _observe_sensors(
            self, snapshot: object, *, defer_trigger: bool = False
        ) -> None:
            order.append("observe")
            self.observed.append((snapshot, defer_trigger))

        def _render_capability_update(self, update: object) -> None:
            order.append("render")

    window: Any = Window()
    outcome.append(CapabilityUpdate())  # the flow rejected the result
    window._on_probe_done(1, ProbeRequest("sane:0", ()), {}, 0)
    assert window.observed == []
    assert order == ["flow", "render"]

    order.clear()
    outcome[0] = CapabilityUpdate(sensor_caps={})
    window._on_probe_done(1, ProbeRequest("sane:0", ()), {}, 0)
    assert order == ["flow", "observe", "render"]  # accepted, before rendering
    assert window.observed[0][1] is True  # the trigger is deferred


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_hidden_window_consumes_a_trigger_instead_of_scanning() -> None:
    # Polls skip their tick while the window is hidden, so a read already
    # in flight when it went away must not start a scan either. The edge
    # is consumed, which is what keeps it from firing on return.
    window = _duck_window(mapping="same")
    window.is_suspended = lambda: True
    window._observe_sensors(_IDLE)

    window._observe_sensors(_BUTTON)
    assert window.started == []

    window.is_suspended = lambda: False
    window._observe_sensors(_BUTTON)  # still latched: no new edge
    assert window.started == []
    window._observe_sensors(_IDLE)
    window._observe_sensors(_BUTTON)
    assert window.started == ["collect"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_polling_wanted_requires_prefs_predicate_probe_idle_and_sensors() -> None:
    from scanmole.options import Capability
    from scanmole_gui.app import MainWindow

    class Window:
        _sensor_polling_wanted = MainWindow._sensor_polling_wanted
        _sensor_prefs = MainWindow._sensor_prefs

        def __init__(self) -> None:
            self._settings: dict[str, object] = {"hardware_button": "same"}
            self._released = False
            self._closing = False
            self.allowed = True

            class Flow:
                def __init__(self) -> None:
                    self.probe_active = False
                    self.last_caps: dict[str, object] | None = {
                        "scan": Capability(kind="bool", current="no")
                    }

            self._flow = Flow()

        def _scan_allowed(self) -> bool:
            return self.allowed

    window: Any = Window()
    assert window._sensor_polling_wanted() is True

    # Every trigger off: no polling at all, whatever the device offers.
    window._settings = {"hardware_button": "off"}
    assert window._sensor_polling_wanted() is False
    window._settings = {"hardware_button": "off", "insert_to_scan": True}
    # ... and no paper level to watch for the insertion either.
    assert window._sensor_polling_wanted() is False
    window._flow.last_caps = {"page-loaded": Capability(kind="bool", current="no")}
    assert window._sensor_polling_wanted() is True
    window._flow.last_caps = {"scan": Capability(kind="bool", current="no")}
    window._settings = {}

    window.allowed = False  # scan running, searching, no device, blocked CLI
    assert window._sensor_polling_wanted() is False
    window.allowed = True

    window._flow.probe_active = True  # a capability probe owns the device
    assert window._sensor_polling_wanted() is False
    window._flow.probe_active = False

    window._flow.last_caps = {}  # no usable sensors: never a device list
    assert window._sensor_polling_wanted() is False
    window._flow.last_caps = None
    assert window._sensor_polling_wanted() is False


def _polling_window(settings: dict[str, object], caps: dict[str, Any] | None) -> Any:
    """A window stub carrying only what the polling predicate reads."""
    from scanmole_gui.app import MainWindow

    class Window:
        _sensor_polling_wanted = MainWindow._sensor_polling_wanted
        _sensor_prefs = MainWindow._sensor_prefs

        def __init__(self) -> None:
            self._settings = settings
            self._released = False
            self._closing = False
            self._flow = type("Flow", (), {"probe_active": False, "last_caps": caps})()

        def _scan_allowed(self) -> bool:
            return True

    return Window()


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    ("mapping", "insert", "sensors", "wanted"),
    [
        # A preference is worth polling for only where the device has the
        # sensor that would answer it.
        ("same", False, ("scan",), True),
        ("single", False, ("scan",), True),
        ("collect", False, ("scan",), True),
        ("same", False, ("page-loaded",), False),
        ("same", False, (), False),
        ("same", True, ("page-loaded",), True),  # the insertion carries it
        ("off", True, ("page-loaded",), True),
        ("off", True, ("scan",), False),
        ("off", False, ("scan", "page-loaded"), False),
        ("same", False, ("scan", "page-loaded"), True),
    ],
)
def test_polling_needs_a_sensor_for_the_enabled_preference(
    mapping: str, insert: bool, sensors: tuple[str, ...], wanted: bool
) -> None:
    from scanmole.options import Capability

    window = _polling_window(
        {"hardware_button": mapping, "insert_to_scan": insert},
        {name: Capability(kind="bool", current="no") for name in sensors},
    )

    assert window._sensor_polling_wanted() is wanted


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_inconclusive_sensor_evidence_never_enables_polling() -> None:
    # Sensor recognition is exact: an inactive option, a non-boolean one
    # or an unparseable value is unavailable evidence, not a scan button.
    from scanmole.options import Capability

    for capability in (
        Capability(kind="bool", current="no", active=False),
        Capability(kind="enum", choices=["yes", "no"], current="no"),
        Capability(kind="bool", current="<yes|no>"),
    ):
        window = _polling_window({"hardware_button": "same"}, {"scan": capability})
        assert window._sensor_polling_wanted() is False


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
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
        window = _polling_window(dict(saved), None)
        assert window._sensor_prefs()[0] == expected
        assert hardware_button_value(saved) == expected


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
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

    window = _duck_window(mapping="same", form=form)
    window._observe_sensors(_IDLE)  # baseline
    window._observe_sensors(_BUTTON)

    assert window.started == [expected]
    assert form.persisted_values() == before  # nothing was written back


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_paper_level_alone_never_starts_a_scan_for_the_button() -> None:
    # The default mapping must not turn a paper-presence scanner into an
    # auto-start one: only insert-to-scan reads that sensor.
    button_only = _duck_window(mapping="same", insert=False)
    button_only._observe_sensors(_IDLE)
    button_only._observe_sensors(_PAPER)
    assert button_only.started == []

    with_insert = _duck_window(mapping="same", insert=True)
    with_insert._observe_sensors(_IDLE)
    with_insert._observe_sensors(_PAPER)
    assert with_insert.started == ["collect"]


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_snapshot_without_sensor_evidence_starts_nothing() -> None:
    window = _duck_window(mapping="same", insert=True)
    window._observe_sensors(SensorSnapshot())  # baseline
    window._observe_sensors(SensorSnapshot())

    assert window.started == []
    assert _polling_window({}, {})._sensor_polling_wanted() is False
