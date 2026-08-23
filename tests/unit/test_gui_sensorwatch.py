"""Tests for the idle sensor gate and arming rules (no GTK, no hardware)."""

from __future__ import annotations

import threading
from typing import Any

from scanmole.sensors import SensorSnapshot
from scanmole_gui.sensorwatch import AdvisoryGate, Observation, SensorArbiter

_IDLE = SensorSnapshot(scan=False, page_loaded=False)
_BUTTON = SensorSnapshot(scan=True, page_loaded=False)
_PAPER = SensorSnapshot(scan=False, page_loaded=True)
_BOTH = SensorSnapshot(scan=True, page_loaded=True)


def test_gate_serializes_sensor_and_priority_access() -> None:
    gate = AdvisoryGate()

    assert gate.try_acquire("sensor")
    assert not gate.try_acquire("sensor")  # one at a time
    assert not gate.acquire("probe", timeout=0.05)  # held: priority times out
    gate.release()
    assert gate.acquire("probe", timeout=0.05)
    assert not gate.try_acquire("sensor")  # priority work owns the device
    gate.release()
    assert gate.try_acquire("sensor")


def test_gate_priority_waiters_starve_sensor_polls() -> None:
    # While discovery or a probe waits for the gate, a sensor poll must
    # not sneak in ahead of it after the release.
    gate = AdvisoryGate()
    assert gate.try_acquire("sensor")
    got_priority = threading.Event()

    def priority() -> None:
        assert gate.acquire("discovery", timeout=5.0)
        got_priority.set()

    waiter = threading.Thread(target=priority)
    waiter.start()
    for _ in range(100):
        if not gate.try_acquire("sensor"):
            break
    assert not gate.try_acquire("sensor")  # refused while priority waits
    gate.release()
    assert got_priority.wait(5.0)
    gate.release()
    waiter.join(5.0)
    assert gate.try_acquire("sensor")


def test_the_first_observation_is_a_baseline_and_never_triggers() -> None:
    arbiter = SensorArbiter()

    assert arbiter.observe(_BOTH) == Observation()  # latched press + loaded sheet


def test_a_fresh_button_edge_triggers_once() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_BUTTON) == Observation(button=True)
    # A backend keeping the value latched across reads yields no second
    # trigger: one edge, one trigger.
    assert arbiter.observe(_BUTTON) == Observation()
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_BUTTON) == Observation(button=True)


def test_insert_needs_a_real_no_to_yes_transition() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_PAPER) == Observation(insert=True)
    assert arbiter.observe(_PAPER) == Observation()  # level, not a new edge
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_PAPER) == Observation(insert=True)


def test_paper_needs_an_observed_empty_level_to_be_an_insertion() -> None:
    # Without one, a yes could just as well be a sheet that was lying
    # there all along.
    arbiter = SensorArbiter()
    arbiter.observe(SensorSnapshot(scan=False, page_loaded=None))

    assert arbiter.observe(_PAPER) == Observation()


def test_unavailable_reads_do_not_break_an_insertion_chain() -> None:
    # Empty, then a poll the sensor did not answer, then paper: the sheet
    # went in between those reads, whatever the gap. Forgetting the empty
    # level here would drop a real insertion for no safety gain.
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)
    arbiter.observe(SensorSnapshot(scan=False, page_loaded=None))

    assert arbiter.observe(_PAPER) == Observation(insert=True)


def test_button_and_insertion_in_one_observation_report_both() -> None:
    # The caller resolves the priority (an explicit button mapping wins);
    # the arbiter reports every fresh edge it saw.
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.observe(_BOTH) == Observation(button=True, insert=True)


def test_reset_makes_the_next_observation_a_baseline() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)
    arbiter.reset()

    # A press latched while a scan ran resumes as baseline state.
    assert arbiter.observe(_BUTTON) == Observation()


def test_offline_logs_once_per_outage_and_drops_the_baseline() -> None:
    arbiter = SensorArbiter()
    arbiter.observe(_IDLE)

    assert arbiter.mark_offline() is True
    assert arbiter.mark_offline() is False  # no per-second error storm
    # Recovery re-baselines: stale latches from the outage never trigger.
    assert arbiter.observe(_BUTTON) == Observation()
    assert arbiter.mark_offline() is True  # a new outage logs again


# ---- window-level trigger mapping (duck-typed, needs the app module) ------

import importlib.util  # noqa: E402

import pytest  # noqa: E402

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


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_closing_asks_only_when_captured_pages_would_be_lost() -> None:
    # The prompt exists to stop a discard, so it must not appear for a
    # close that loses nothing: an idle window, or a run that has not
    # delivered a page yet.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.session import SessionState

    class Runner:
        def __init__(self) -> None:
            self.running = True

        def is_running(self) -> bool:
            return self.running

    class Window:
        _close_discards_pages = MainWindow._close_discards_pages

        def __init__(self) -> None:
            self._runner: Any = Runner()
            self._session = SessionState(drop_blanks=True, pages=2)
            self._closing = False
            self._close_confirmed = False

    window: Any = Window()
    assert window._close_discards_pages() is True

    window._session = SessionState(drop_blanks=True, pages=0)  # nothing captured yet
    assert window._close_discards_pages() is False
    window._session = SessionState(drop_blanks=True, pages=2)

    window._runner.running = False  # the scan already finished
    assert window._close_discards_pages() is False
    window._runner.running = True

    window._runner = None  # idle window
    assert window._close_discards_pages() is False
    window._runner = Runner()

    window._closing = True  # the close is already under way
    assert window._close_discards_pages() is False
    window._closing = False

    window._close_confirmed = True  # answered once; never asked twice
    assert window._close_discards_pages() is False


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_confirmed_discard_closes_and_echoes_the_log_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The engine keeps preserving after the window is hidden, and its
    # recovery command is the only way back to those pages; the log pane
    # it would land in is gone.
    from scanmole_gui.app import MainWindow

    class Window:
        _on_close_confirm_response = MainWindow._on_close_confirm_response
        _append_log = MainWindow._append_log

        def __init__(self) -> None:
            self._close_confirmed = False
            self._echo_log = False
            self.closed = 0

            class Log:
                def __init__(self) -> None:
                    self.lines: list[str] = []

                def append(self, text: str) -> None:
                    self.lines.append(text)

            self._log = Log()

        def close(self) -> None:
            self.closed += 1

    window: Any = Window()
    window._on_close_confirm_response(None, "keep")
    assert window.closed == 0 and window._echo_log is False

    window._append_log("[gui] before the close")
    assert capsys.readouterr().err == ""

    window._on_close_confirm_response(None, "close")
    assert window.closed == 1
    window._append_log("kept in /tmp/scanmole-x (recover with: ...)")

    assert "recover with" in capsys.readouterr().err
    assert window._log.lines[-1].startswith("kept in")  # still in the pane


def test_probe_evidence_feeds_the_arbiter_without_a_synthetic_edge() -> None:
    # The whole chain, GTK-free: capability flow to arbiter. Device
    # selection probes bare and then source-applied; with paper already
    # loaded those two listings disagree only because they describe
    # different sources. Nothing may trigger from that, while a genuine
    # transition seen later by the idle poller still must.
    from scanmole.options import Capability
    from scanmole.sensors import assess_sensors
    from scanmole_gui.probing import CapabilityFlow

    def caps(page_loaded: str) -> dict[str, Capability]:
        return {
            "source": Capability(kind="enum", choices=["ADF Duplex", "Flatbed"]),
            "page-loaded": Capability(kind="bool", current=page_loaded),
        }

    flow = CapabilityFlow(preferred_source="adf-duplex")
    arbiter = SensorArbiter()
    observed: list[Observation] = []

    def feed(update: Any) -> None:
        if update.sensor_caps is not None:
            observed.append(arbiter.observe(assess_sensors(update.sensor_caps)))

    started = flow.select_device("dev-a", False, "adf-duplex")
    assert started.start_probe is not None
    token, request = started.start_probe
    bare = flow.probe_completed(token, request, caps("no"), "dev-a", "adf-duplex")
    feed(bare)
    assert bare.start_probe is not None
    adf_token, adf_request = bare.start_probe
    applied = flow.probe_completed(
        adf_token, adf_request, caps("yes"), "dev-a", "adf-duplex"
    )
    feed(applied)

    # One observation only (the selected source's), and a baseline never
    # triggers, so the loaded sheet stays state rather than a request.
    assert observed == [Observation()]

    # A genuine transition afterwards still arms exactly once.
    assert arbiter.observe(_IDLE) == Observation()
    assert arbiter.observe(_PAPER) == Observation(insert=True)
    assert arbiter.observe(_PAPER) == Observation()


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_restart_reaches_the_application_only_through_a_real_close() -> None:
    # Restart is a request to close and come back. While the close is
    # still open to refusal the intent stays in the window, or a declined
    # restart would re-execute on some later, unrelated quit.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.session import SessionState

    class App:
        def __init__(self) -> None:
            self.restart_requested = False

    class Runner:
        def is_running(self) -> bool:
            return True

    class Window:
        _on_restart_clicked = MainWindow._on_restart_clicked
        _close_discards_pages = MainWindow._close_discards_pages
        _on_close_confirm_response = MainWindow._on_close_confirm_response
        _append_log = MainWindow._append_log

        def __init__(self, pages: int) -> None:
            self._app = App()
            self._settings_dialog = None
            self._runner: Any = Runner()
            self._session = SessionState(drop_blanks=True, pages=pages)
            self._closing = False
            self._close_confirmed = False
            self._restart_pending = False
            self._echo_log = False
            self._log = type("Log", (), {"append": lambda self, text: None})()
            self.prompts = 0

        def get_application(self) -> App:
            return self._app

        def restart_reached_the_app(self) -> bool:
            return self._app.restart_requested

        def close(self) -> None:
            # Stands in for GTK dispatching close-request, whose real
            # handler transfers the intent once the close goes through.
            window: Any = self
            if window._close_discards_pages():
                self.prompts += 1
                return
            if self._restart_pending:
                self._app.restart_requested = True

    plain: Any = Window(pages=0)  # nothing captured: no question asked
    plain._on_restart_clicked()
    assert plain.prompts == 0
    assert plain.restart_reached_the_app() is True

    confirmed: Any = Window(pages=3)
    confirmed._on_restart_clicked()
    assert confirmed.prompts == 1  # inhibited by the discard prompt
    assert confirmed.restart_reached_the_app() is False
    confirmed._on_close_confirm_response(None, "close")
    assert confirmed.restart_reached_the_app() is True

    # "Keep Scanning" leaves nothing behind for a later ordinary quit.
    # Dismissing the dialog arrives here as the same response, because
    # ``_confirm_close`` registers "keep" as its close response.
    declined: Any = Window(pages=3)
    declined._on_restart_clicked()
    declined._on_close_confirm_response(None, "keep")
    assert declined._restart_pending is False
    declined._session = SessionState(drop_blanks=True, pages=0)
    declined.close()  # an ordinary close, much later
    assert declined.restart_reached_the_app() is False


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
