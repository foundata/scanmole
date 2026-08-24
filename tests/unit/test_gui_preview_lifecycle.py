"""Tests for the window-side preview lifecycle (no GTK loop, no hardware).

``PreviewFlow`` owns the debounce, the worker generation and the folder
monitor; the window decides when to ask and what a look is about. What a
single look actually reports is pinned in ``test_preview.py``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
]


_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)


class _Loop:
    """A stand-in for the GLib sources the preview lifecycle uses."""

    def __init__(self) -> None:
        self.timeouts: dict[int, Callable[[], bool]] = {}
        self.idles: list[Callable[..., bool]] = []
        self.removed: list[int] = []
        self._next = 1

    def timeout_add(self, _ms: int, callback: Callable[[], bool]) -> int:
        token = self._next
        self._next += 1
        self.timeouts[token] = callback
        return token

    def source_remove(self, token: int) -> None:
        self.removed.append(token)
        self.timeouts.pop(token, None)

    def idle_add(self, callback: Callable[..., bool], *args: object) -> int:
        self.idles.append(lambda: callback(*args))
        return 0

    def fire_timeouts(self) -> None:
        """Run every armed timeout, dropping the one-shot ones as GLib does."""
        for token, callback in list(self.timeouts.items()):
            if not callback():  # GLib.SOURCE_REMOVE
                self.timeouts.pop(token, None)

    def fire_idles(self) -> None:
        pending, self.idles = self.idles, []
        for callback in pending:
            callback()


def _preview_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: _Loop, *, hold: bool = False
) -> Any:
    """A window stub carrying only the preview lifecycle, no GTK loop.

    With ``hold``, a started worker waits for an explicit ``release()``
    before reporting, so a test can order two workers against each other
    without any timing.
    """
    from scanmole_gui.app import MainWindow
    from scanmole_gui.previewflow import PreviewFlow

    monkeypatch.setattr("scanmole_gui.previewflow.GLib.timeout_add", loop.timeout_add)
    monkeypatch.setattr(
        "scanmole_gui.previewflow.GLib.source_remove", loop.source_remove
    )
    monkeypatch.setattr("scanmole_gui.previewflow.GLib.idle_add", loop.idle_add)

    class Monitor:
        def __init__(self, folder: Path) -> None:
            self.folder = folder
            self.cancelled = False
            self.handler: Callable[..., None] | None = None

        def connect(self, _signal: str, handler: Callable[..., None]) -> None:
            self.handler = handler

        def cancel(self) -> None:
            self.cancelled = True

    monitors: list[Monitor] = []

    def _make_monitor(folder: Path) -> Monitor:
        monitor = Monitor(folder)
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(
        PreviewFlow, "_create_directory_monitor", staticmethod(_make_monitor)
    )

    class Window:
        # The window keeps deciding when to ask and what a look is about;
        # the flow owns everything between the request and the rendered
        # line. Both halves are the production ones.
        _preview_alive = MainWindow._preview_alive
        _preview_inputs = MainWindow._preview_inputs
        _on_visible_changed = MainWindow._on_visible_changed
        _on_active_changed = MainWindow._on_active_changed

        def __init__(self) -> None:
            self._released = False
            self._closing = False
            self.device: str | None = "test:0"
            self.visible = True
            self.active = True
            self.rendered: list[str] = []
            self.monitors = monitors
            self.started = 0
            self._pending_work: list[Callable[[], None]] = []
            # Visibility changes also poke the device controller; the
            # preview tests only need it to accept the call.
            self._deviceflow = type(
                "D", (), {"view_state_changed": staticmethod(lambda: None)}
            )()

            window = self

            class Form:
                folder = staticmethod(lambda: str(tmp_path))
                preview_template = staticmethod(lambda: "scan_{NNN}.pdf")

                @staticmethod
                def set_preview(text: str) -> None:
                    window.rendered.append(text)

            self._form = Form()
            self._preview = PreviewFlow(
                alive=self._preview_alive,  # type: ignore[misc]
                inputs=self._preview_inputs,  # type: ignore[misc]
                render=lambda text: self._form.set_preview(text),
            )

        def _selected_device(self) -> str | None:
            return self.device

        # The lifecycle state the assertions below read. Reaching into the
        # flow happens here alone, so a rename stays a one-line change.
        @property
        def monitor(self) -> Any:
            return self._preview._monitor

        @property
        def debounce_id(self) -> int | None:
            return self._preview._debounce_id

        @property
        def rerun_pending(self) -> bool:
            return self._preview._again

        def get_visible(self) -> bool:
            return self.visible

        def is_active(self) -> bool:
            return self.active

        @property
        def held(self) -> bool:
            return bool(self._pending_work)

        def release(self) -> None:
            """Let the oldest held worker report its result."""
            self._pending_work.pop(0)()

    window: Any = Window()

    # The worker body runs inline so the lifecycle stays deterministic;
    # the production path only differs in which thread computes it.
    def inline(target: Callable[[], None], **_kwargs: object) -> Any:
        def start() -> None:
            window.started += 1
            if hold:
                window._pending_work.append(target)
            else:
                target()

        return type("Thread", (), {"start": staticmethod(start)})()

    monkeypatch.setattr("scanmole_gui.previewflow.threading.Thread", inline)

    return window


def _settle(window: Any, loop: _Loop) -> None:
    """Run the debounce and the worker completion to quiescence."""
    loop.fire_timeouts()
    loop.fire_idles()


@_NEEDS_GI
def test_the_preview_reflects_what_is_already_in_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    (tmp_path / "scan_001.pdf").touch()

    window._preview.request()
    _settle(window, loop)

    assert window.rendered == ["scan_002.pdf"]
    # Looking never reserves: only the file the test created is there.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scan_001.pdf"]


@_NEEDS_GI
def test_a_burst_of_events_collapses_into_one_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)

    for _ in range(5):  # an atomic replace emits several monitor events
        window._preview.request()

    assert len(loop.timeouts) == 1  # four were cancelled again
    assert len(loop.removed) == 4
    _settle(window, loop)
    assert window.rendered == ["scan_001.pdf"]


@_NEEDS_GI
def test_a_monitor_event_advances_the_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    window._preview.request()
    _settle(window, loop)
    assert window.rendered == ["scan_001.pdf"]

    (tmp_path / "scan_001.pdf").touch()  # another process took the name
    monitor = window.monitor
    assert monitor.handler is not None
    monitor.handler(monitor)
    _settle(window, loop)

    assert window.rendered[-1] == "scan_002.pdf"


@_NEEDS_GI
def test_a_worker_finishing_after_its_inputs_changed_never_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real ordering: A is still inspecting the old folder when the
    # user picks a new one, and A finishes before B's debounce fires.
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop, hold=True)
    (tmp_path / "scan_001.pdf").touch()
    other = tmp_path / "other"
    other.mkdir()

    window._preview.request()
    loop.fire_timeouts()  # A starts and is held before it reports
    assert window.held, "worker A should be in flight"

    window._form.folder = staticmethod(lambda: str(other))
    window._preview.request()  # B is debounced; A is now stale

    window.release()  # A completes first
    loop.fire_idles()
    assert window.rendered == []  # A described a folder nobody selected

    loop.fire_timeouts()  # B starts
    window.release()
    loop.fire_idles()

    assert window.rendered == ["scan_001.pdf"]  # only B, against the new folder


@_NEEDS_GI
def test_changes_during_one_look_collapse_into_a_single_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop, hold=True)

    window._preview.request()
    loop.fire_timeouts()  # A is in flight
    for _ in range(3):  # three changes while A works
        window._preview.request()
        loop.fire_timeouts()  # each debounce fires into the busy worker
    assert window.rerun_pending is True
    assert window.started == 1  # no second worker was ever launched

    window.release()  # A completes, stale, and hands over to the rerun
    loop.fire_idles()

    assert window.started == 2  # exactly one rerun, with the newest inputs
    window.release()
    loop.fire_idles()
    assert window.rendered == ["scan_001.pdf"]


@_NEEDS_GI
def test_teardown_invalidates_an_active_worker_without_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop, hold=True)
    window._preview.request()
    loop.fire_timeouts()
    window._preview.request()  # a rerun would be pending
    loop.fire_timeouts()
    assert window.rerun_pending is True

    window._preview.stop()
    window.release()
    loop.fire_idles()

    assert window.rendered == []
    assert window.started == 1  # the pending rerun was dropped too


@_NEEDS_GI
def test_hiding_the_window_stops_watching_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop, hold=True)
    window._preview.request()
    loop.fire_timeouts()  # A is in flight
    monitor = window.monitor
    assert monitor is not None

    window.visible = False
    window._on_visible_changed()

    assert monitor.cancelled is True
    assert window.monitor is None
    assert loop.timeouts == {}  # no worker, no debounce armed
    monitor.handler(monitor)  # a late event from the dropped monitor
    assert loop.timeouts == {}

    window.release()  # A finishes for a window nobody is looking at
    loop.fire_idles()
    assert window.rendered == []

    window.visible = True
    window._on_visible_changed()
    loop.fire_timeouts()
    window.release()
    loop.fire_idles()

    assert window.rendered == ["scan_001.pdf"]
    assert window.monitor is not monitor  # a fresh one


@_NEEDS_GI
def test_focus_alone_neither_refreshes_nor_drops_the_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    window._preview.request()
    _settle(window, loop)
    monitor = window.monitor

    window.active = False
    window._on_active_changed()

    assert loop.timeouts == {}  # losing focus asks for no filesystem work
    assert window.monitor is monitor  # still the right folder

    window.active = True
    window._on_active_changed()

    assert loop.timeouts != {}  # regaining it does ask


@_NEEDS_GI
def test_replacing_the_folder_stops_the_previous_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    window._preview.request()
    _settle(window, loop)
    first = window.monitor
    assert first is not None

    other = tmp_path / "other"
    other.mkdir()
    window._form.folder = staticmethod(lambda: str(other))
    window._preview.request()
    _settle(window, loop)

    assert first.cancelled is True
    assert window.monitor is not first
    # An event from the old monitor is not this folder's news.
    before = list(window.rendered)
    window._preview._on_folder_changed(first)
    assert window.debounce_id is None
    assert window.rendered == before


@_NEEDS_GI
def test_teardown_stops_the_monitor_and_the_pending_debounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    window._preview.request()
    _settle(window, loop)
    monitor = window.monitor
    window._preview.request()  # a debounce is armed again
    assert window.debounce_id is not None

    window._preview.stop()

    assert monitor.cancelled is True
    assert window.monitor is None
    assert window.debounce_id is None
    assert loop.timeouts == {}
    # Anything still in flight is now stale by generation.
    window._released = True
    window._preview.request()
    assert loop.timeouts == {}


@_NEEDS_GI
def test_nothing_late_reaches_a_closed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Close is the ordering the window really uses: it releases first and
    # tears the flow down after. Everything still in flight at that point
    # (a worker, the monitor's next event, the pending rerun) has to land
    # on widgets that may already be gone, so none of it may render or arm
    # anything.
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop, hold=True)
    window._preview.request()
    loop.fire_timeouts()  # a worker is in flight
    monitor = window.monitor
    assert monitor is not None and window.held

    window._released = True  # the close request, in its own order
    window._preview.stop()

    assert monitor.cancelled is True  # the monitor goes with the window
    assert window.monitor is None and window.debounce_id is None
    monitor.handler(monitor)  # a late directory event
    assert loop.timeouts == {}  # nothing armed for a window that is going
    window.release()  # the worker reports into the closed window
    loop.fire_idles()

    assert window.rendered == []
    assert window.started == 1  # and no rerun was launched behind it


@_NEEDS_GI
def test_an_unavailable_folder_renders_a_state_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)
    missing = tmp_path / "not-there"
    window._form.folder = staticmethod(lambda: str(missing))

    window._preview.request()
    _settle(window, loop)

    assert window.rendered == ["folder not found"]
    assert not missing.exists()  # previewing never creates the folder

    missing.mkdir()  # the user corrects it
    window._preview.request()
    _settle(window, loop)
    assert window.rendered[-1] == "scan_001.pdf"


@_NEEDS_GI
def test_the_preview_arms_no_repeating_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The debounce fires once and removes itself; nothing polls the
    # filesystem on a clock, so {ss} does not tick either.
    loop = _Loop()
    window = _preview_window(tmp_path, monkeypatch, loop)

    window._preview.request()
    token = window.debounce_id
    assert token is not None
    assert loop.timeouts[token]() is False  # GLib.SOURCE_REMOVE
    loop.fire_idles()

    assert window.debounce_id is None
    assert len(window.rendered) == 1
