"""Tests for the advisory command supervisor (no GTK, real children)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from typing import Any

import pytest

from scanmole.external import run_command
from scanmole_gui.advisory import AdvisoryCommands

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None, reason="needs PyGObject"
)


def _run_advisory(
    supervisor: AdvisoryCommands,
    generation: int,
    argv: list[str],
    results: list[object],
) -> None:
    try:
        done = run_command(
            argv, timeout_seconds=20, on_spawn=supervisor.adopter(generation)
        )
        results.append(done.returncode)
    except Exception as exc:
        results.append(exc)


def _wait_for_child(supervisor: AdvisoryCommands) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with supervisor._lock:
            if supervisor._processes:
                return next(iter(supervisor._processes))
        time.sleep(0.01)
    raise AssertionError("advisory child never registered")


def test_cancel_stops_a_running_advisory_child_boundedly() -> None:
    supervisor = AdvisoryCommands()
    results: list[object] = []
    supervisor.spawn_worker(
        _run_advisory,
        supervisor,
        supervisor.generation,
        ["sh", "-c", "sleep 30"],
        results,
    )
    pid = _wait_for_child(supervisor)

    started = time.monotonic()
    idle = supervisor.cancel_pending()

    assert idle is True
    assert time.monotonic() - started < 5  # bounded, nowhere near the sleep
    assert results and results[0] == -15  # the worker saw the group TERM
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped: nothing owns the scanner anymore


def test_cancel_escalates_to_kill_for_a_term_ignoring_child() -> None:
    supervisor = AdvisoryCommands()
    results: list[object] = []
    supervisor.spawn_worker(
        _run_advisory,
        supervisor,
        supervisor.generation,
        ["sh", "-c", 'trap "" TERM; sleep 30'],
        results,
    )
    _wait_for_child(supervisor)

    started = time.monotonic()
    idle = supervisor.cancel_pending()

    assert idle is True
    assert time.monotonic() - started < 6  # TERM grace plus the KILL bound
    assert results and results[0] == -9


def test_cancellation_bumps_the_generation_and_idles_cheaply() -> None:
    supervisor = AdvisoryCommands()

    before = supervisor.generation
    started = time.monotonic()
    assert supervisor.cancel_pending() is True  # nothing to do
    assert time.monotonic() - started < 0.5
    assert supervisor.generation == before + 1


def test_a_late_spawn_after_a_closing_cancel_is_killed() -> None:
    supervisor = AdvisoryCommands()
    supervisor.cancel_pending(close=True)

    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    supervisor.adopt(process, supervisor.generation)  # raced the close

    assert process.wait(timeout=5) == -9


def test_a_spawn_with_a_stale_generation_is_killed() -> None:
    # A non-closing cancellation (a scan takeover) also invalidates
    # in-flight workers: whatever they still spawn dies on adoption.
    supervisor = AdvisoryCommands()
    adopt = supervisor.adopter(supervisor.generation)
    supervisor.cancel_pending()

    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    adopt(process)

    assert process.wait(timeout=5) == -9


def test_a_worker_resuming_after_the_cancel_cannot_leak_a_child() -> None:
    # The realistic interleaving: the version probe's child is killed by
    # the takeover, the discovery worker resumes and launches the device
    # listing; that late child must not survive into the scan.
    import threading

    supervisor = AdvisoryCommands()
    generation = supervisor.generation
    adopt = supervisor.adopter(generation)
    first = subprocess.Popen(["sleep", "30"], start_new_session=True)
    adopt(first)
    resumed = threading.Event()
    second: list[subprocess.Popen[bytes]] = []

    def worker() -> None:
        resumed.wait(10)  # past the takeover: the first command died
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        adopt(process)
        second.append(process)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    supervisor.cancel_pending()  # the scan takeover
    resumed.set()
    thread.join(5)

    assert first.wait(timeout=5) < 0  # the snapshotted child died
    assert second and second[0].wait(timeout=5) == -9  # and the late one too


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_scan_start_cancels_advisory_work_before_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The engine probes the device authoritatively at scan start; the
    # advisory children must be gone before the runner launches.
    from scanmole_gui.app import MainWindow

    order: list[str] = []

    class Advisory:
        def cancel_pending(self, **_kw: object) -> bool:
            order.append("cancel")
            return True

    class Flow:
        def reset(self) -> None:
            order.append("reset")

    class Form:
        def folder(self) -> str:
            return "/tmp"

        def scan_request(self, device: object, folder: object) -> Any:
            from scanmole_gui.request import ScanRequest

            return ScanRequest(
                device=None,
                source="adf-duplex",
                mode="lineart",
                resolution=300,
                page_size="auto",
                auto_size_preference="iso",
                ocr=False,
                lang="deu",
                deskew=False,
                drop_blanks=True,
                output=str(folder) + "/out.pdf",
            )

        def set_running(self, running: bool) -> None:
            order.append("running")

    class Runner:
        def __init__(self, **_kw: object) -> None:
            pass

        def start(self, argv: object, cwd: object) -> None:
            order.append("start")

    class Window:
        _on_scan_clicked = MainWindow._on_scan_clicked

        def __init__(self) -> None:
            self._runner = None
            self._advisory = Advisory()
            self._flow = Flow()
            self._searching = True
            self._form = Form()
            self._scanmole = "scanmole"
            self._session = None
            self._run_folder = None
            self._last_output = None
            self._schedule = lambda cb: None
            self._after_seconds = lambda s, cb: None
            self._on_stdout_line = lambda r, line: None
            self._on_stderr_line = lambda r, line: None
            self._on_process_exit = lambda r, code: None
            self._on_kill_escalated = lambda r: None

        def _selected_device(self) -> None:
            return None

        def _save_settings(self) -> None:
            order.append("save")

        def _append_log(self, text: str) -> None:
            pass

        def _set_result_bar(self, *a: object, **k: object) -> None:
            pass

    window = Window()
    monkeypatch.setattr("scanmole_gui.app.ScanRunner", Runner)
    window._on_scan_clicked()  # type: ignore[misc]

    assert order.index("cancel") < order.index("reset") < order.index("start")
    assert window._searching is False  # a cancelled search cannot stay latched


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_no_advisory_child_survives_into_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End to end with the real supervisor: a live advisory child at scan
    # click is gone by the time the runner starts.
    from scanmole_gui.app import MainWindow
    from scanmole_gui.probing import CapabilityFlow
    from scanmole_gui.request import ScanRequest

    supervisor = AdvisoryCommands()
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    supervisor.adopt(child, supervisor.generation)
    seen: list[int | None] = []

    class Form:
        def folder(self) -> str:
            return "/tmp"

        def scan_request(self, device: object, folder: object) -> Any:
            return ScanRequest(
                device=None,
                source="adf-duplex",
                mode="lineart",
                resolution=300,
                page_size="auto",
                auto_size_preference="iso",
                ocr=False,
                lang="deu",
                deskew=False,
                drop_blanks=True,
                output=str(folder) + "/out.pdf",
            )

        def set_running(self, running: bool) -> None:
            pass

    class Runner:
        def __init__(self, **_kw: object) -> None:
            pass

        def start(self, argv: object, cwd: object) -> None:
            seen.append(child.poll())  # must be reaped already

    class Window:
        _on_scan_clicked = MainWindow._on_scan_clicked

        def __init__(self) -> None:
            self._runner = None
            self._advisory = supervisor
            self._flow = CapabilityFlow()
            self._searching = False
            self._form = Form()
            self._scanmole = "scanmole"
            self._session = None
            self._run_folder = None
            self._last_output = None
            self._schedule = lambda cb: None
            self._after_seconds = lambda s, cb: None
            self._on_stdout_line = lambda r, line: None
            self._on_stderr_line = lambda r, line: None
            self._on_process_exit = lambda r, code: None
            self._on_kill_escalated = lambda r: None

        def _selected_device(self) -> None:
            return None

        def _save_settings(self) -> None:
            pass

        def _append_log(self, text: str) -> None:
            pass

        def _set_result_bar(self, *a: object, **k: object) -> None:
            pass

    monkeypatch.setattr("scanmole_gui.app.ScanRunner", Runner)
    Window()._on_scan_clicked()  # type: ignore[misc]

    assert seen and seen[0] is not None  # no advisory process survived


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_released_or_cancelled_callbacks_never_touch_the_window() -> None:
    # Pending main-loop callbacks may fire after the window released its
    # advisory work (close, shutdown) or after a cancellation invalidated
    # their generation: they must drop without touching any widget (the
    # stand-in has no form or flow, so a touch raises AttributeError).
    from scanmole_gui.app import MainWindow
    from scanmole_gui.probing import ProbeRequest

    class Window:
        _apply_devices = MainWindow._apply_devices
        _on_probe_done = MainWindow._on_probe_done

        def __init__(self, released: bool) -> None:
            self._released = released
            self._advisory = AdvisoryCommands()
            self._searching = True

    released = Window(released=True)
    released._apply_devices([], "", "", released._advisory.generation)  # type: ignore[misc]
    assert released._searching is False  # the latch never sticks

    stale = Window(released=False)
    stale._advisory.cancel_pending()  # bumps the generation
    stale._apply_devices([], "", "", stale._advisory.generation - 1)  # type: ignore[misc]
    assert stale._searching is False
    stale._on_probe_done(  # type: ignore[misc]
        1, ProbeRequest("dev"), None, stale._advisory.generation - 1
    )


@_NEEDS_GI
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_scan_exit_starts_a_fresh_negotiation() -> None:
    # The takeover reset the capability flow; the device is free again
    # once the scan exits, so availability renegotiates right away. A
    # stale runner's exit must not.
    from pathlib import Path

    from scanmole_gui.app import MainWindow
    from scanmole_gui.session import SessionState

    calls: list[str] = []

    class Form:
        def set_running(self, running: bool) -> None:
            pass

    class Window:
        _on_process_exit = MainWindow._on_process_exit

        def __init__(self, runner: object) -> None:
            self._runner = runner
            self._form = Form()
            self._session = SessionState(drop_blanks=True)
            self._run_folder = Path("/nonexistent")
            self._last_output = None

        def _update_scan_enabled(self) -> None:
            pass

        def _append_log(self, text: str) -> None:
            pass

        def _start_negotiation(self) -> None:
            calls.append("negotiate")

        def _set_result_bar(self, *a: object, **k: object) -> None:
            pass

        def _alert(self, *a: object) -> None:
            pass

    runner = object()
    window = Window(runner)
    window._on_process_exit(runner, 0)  # type: ignore[misc, arg-type]
    assert calls == ["negotiate"]
    assert window._runner is None

    calls.clear()
    stale = Window(object())
    stale._on_process_exit(object(), 0)  # type: ignore[misc, arg-type]
    assert calls == []  # a stale exit changes nothing
