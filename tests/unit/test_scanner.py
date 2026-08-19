"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError, ProcessingError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.scanner import (
    EffectiveSettings,
    build_scan_command,
    run_scanimage,
    scan_to_files,
)


def _config(**overrides: object) -> ScanConfig:
    values: dict[str, object] = {
        "device": None,
        "source": "adf-duplex",
        "mode": "lineart",
        "resolution": 300,
        "page_size": "a4",
        "despeckle": 1,
        "deskew": False,
        "crop": False,
        "ocr": False,
        "lang": "deu",
        "rotate_pages": True,
        "optimize": 1,
        "pdfa": False,
        "blank_threshold": 0.995,
        "keep_blanks": False,
        "from_images": None,
        "keep_images": None,
        "output": Path("out.pdf"),
    }
    values.update(overrides)
    return ScanConfig(**values)  # type: ignore[arg-type]


def test_run_scanimage_reports_pages_printed_on_stdout(tmp_path: Path) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    exit_code, stderr = run_scanimage(
        [
            "sh",
            "-c",
            f"echo '{first}'; echo 'Scanned page 1.' >&2; echo '{second}'; exit 7",
        ],
        seen.append,
    )

    assert exit_code == 7
    assert seen == [first, second]
    assert "Scanned page 1." in stderr


def test_run_scanimage_waits_for_slow_page_callbacks(tmp_path: Path) -> None:
    # The process can exit long before the callbacks finish; returning while
    # one still runs would race the caller's post-batch logic against a page
    # that is still being analyzed. All callbacks must complete first.
    import time

    pages = [tmp_path / f"page_{n:04d}.pnm" for n in range(1, 4)]
    handled: list[Path] = []

    def slow_callback(page: Path) -> None:
        time.sleep(0.2)
        handled.append(page)

    script = "; ".join(f"echo '{page}'" for page in pages)
    run_scanimage(["sh", "-c", script], slow_callback)

    assert handled == pages  # complete and in order at the moment of return


def test_run_scanimage_ignores_non_page_stdout_lines(tmp_path: Path) -> None:
    seen: list[Path] = []

    run_scanimage(["sh", "-c", "echo 'not a page'; echo ''"], seen.append)

    assert seen == []


def test_run_scanimage_propagates_page_callback_failures(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"

    def failing_callback(path: Path) -> None:
        raise RuntimeError("callback exploded")

    # The sleep would stall the test for 30s if the failing callback did not
    # terminate the subprocess promptly.
    with pytest.raises(ScanMoleError, match="callback exploded") as info:
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], failing_callback)

    assert isinstance(info.value.__cause__, RuntimeError)


def test_run_scanimage_propagates_domain_errors_unwrapped(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"
    error = ProcessingError("event stream gone")

    def failing_callback(path: Path) -> None:
        raise error

    with pytest.raises(ProcessingError) as info:
        run_scanimage(["sh", "-c", f"echo '{page}'"], failing_callback)

    assert info.value is error


def test_run_scanimage_stops_delivering_after_a_callback_failure(
    tmp_path: Path,
) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    def failing_callback(path: Path) -> None:
        seen.append(path)
        raise RuntimeError("boom")

    with pytest.raises(ScanMoleError):
        run_scanimage(
            ["sh", "-c", f"echo '{first}'; echo '{second}'"], failing_callback
        )

    assert seen == [first]


class _BlockingCallback:
    """A page callback that blocks until released, recording its lifecycle."""

    def __init__(self, hold_seconds: float = 0.25) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.seen: list[str] = []
        self._hold_seconds = hold_seconds

    def __call__(self, page: Path) -> None:
        self.seen.append(page.name)
        if not self.entered.is_set():
            self.entered.set()
            # Release shortly after: long enough that a premature raise is
            # provably premature, short enough to keep the test fast.
            threading.Timer(self._hold_seconds, self.release.set).start()
            self.release.wait(10)
            self.finished.set()


def _interrupting_wait(
    monkeypatch: pytest.MonkeyPatch, trigger: threading.Event
) -> None:
    """Deliver KeyboardInterrupt inside the first ``process.wait()`` call.

    Deterministic stand-in for a SIGINT arriving while the batch runs: the
    scan-timeout wait blocks until the callback has provably entered, then
    raises. Later ``wait()`` calls (the reap) behave normally.
    """
    real_wait = subprocess.Popen.wait
    state = {"armed": True}

    def wait(self: subprocess.Popen[str], timeout: float | None = None) -> int:
        if state["armed"]:
            state["armed"] = False
            assert trigger.wait(10)
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)


def _no_scanner_threads() -> bool:
    return not [
        thread for thread in threading.enumerate() if "scanmole-" in thread.name
    ]


def test_interrupt_waits_for_the_active_callback_and_buffered_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression this change is about: a SIGINT (KeyboardInterrupt)
    # during the batch must not escape run_scanimage while an entered
    # callback still runs, and announcements already in the pipe must be
    # delivered, in order, before the interrupt propagates.
    pages = [tmp_path / f"page_{n:04d}.pnm" for n in (1, 2, 3)]
    callback = _BlockingCallback()
    _interrupting_wait(monkeypatch, callback.entered)
    announce = "; ".join(f"echo '{page}'" for page in pages)

    with pytest.raises(KeyboardInterrupt):
        run_scanimage(["sh", "-c", f"{announce}; sleep 30"], callback)

    assert callback.finished.is_set()  # the entered callback ran to its end
    assert callback.seen == [page.name for page in pages]  # buffered, in order
    assert _no_scanner_threads()  # both readers finished before the raise


def test_timeout_waits_for_the_active_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.scanner.SCAN_TIMEOUT_SECONDS", 0.2)
    page = tmp_path / "page_0001.pnm"
    callback = _BlockingCallback()

    with pytest.raises(DeviceError, match="timed out"):
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], callback)

    assert callback.finished.is_set()
    assert _no_scanner_threads()


def test_interrupt_during_the_drain_is_raised_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Normal completion, but the interrupt lands in the reader join: it
    # must be recorded, the drain must continue, and it must be raised
    # only after every callback and reader finished.
    page = tmp_path / "page_0001.pnm"
    callback = _BlockingCallback()
    real_join = threading.Thread.join
    armed = {"value": True}

    def interrupting_join(self: threading.Thread, timeout: float | None = None) -> None:
        if armed["value"]:
            armed["value"] = False
            raise KeyboardInterrupt
        real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", interrupting_join)

    with pytest.raises(KeyboardInterrupt):
        run_scanimage(["sh", "-c", f"echo '{page}'"], callback)

    assert callback.finished.is_set()
    assert _no_scanner_threads()


def test_reader_startup_failure_still_reaps_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the second reader cannot start, the drain must not retry joining
    # a never-started thread forever: reap the child, join what started,
    # close the pipes and raise the startup failure.
    page = tmp_path / "page_0001.pnm"
    real_start = threading.Thread.start

    def failing_start(self: threading.Thread) -> None:
        if self.name == "scanmole-stderr-reader":
            raise RuntimeError("no more threads")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    with pytest.raises(RuntimeError, match="no more threads"):
        run_scanimage(["sh", "-c", f"echo '{page}'; exec sleep 30"], lambda p: None)

    assert _no_scanner_threads()


def test_persistent_close_failure_is_recorded_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cleanup step that always fails must be recorded once as the
    # terminating cause, never spun on: the run ends with the failure
    # instead of hanging.
    page = tmp_path / "page_0001.pnm"

    def failing_close(stream: object) -> None:
        if hasattr(stream, "close"):
            stream.close()  # really close: no fd may leak to the GC
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr("scanmole.scanner._close_stream", failing_close)
    seen: list[Path] = []

    with pytest.raises(OSError, match="Bad file descriptor"):
        run_scanimage(["sh", "-c", f"echo '{page}'"], seen.append)

    assert seen == [page]  # the batch itself completed before cleanup failed
    assert _no_scanner_threads()


def test_build_scan_command_uses_batch_print(tmp_path: Path) -> None:
    command, effective = build_scan_command(
        _config(), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-print" in command
    assert effective == EffectiveSettings(source=None, mode=None, resolution=None)


def test_build_scan_command_auto_size_requests_the_full_window(
    tmp_path: Path,
) -> None:
    caps = {
        "x": Capability(kind="range", minimum=0.0, maximum=215.9),
        "y": Capability(kind="range", minimum=0.0, maximum=3098.8),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "215.9"
    assert command[command.index("-y") + 1] == "3098.8"


def test_build_scan_command_auto_size_clamps_the_area_to_the_page_limits(
    tmp_path: Path,
) -> None:
    # fujitsu backends advertise the -x/-y ranges of the *current* window
    # (A4 height) and only extend them once --page-width/--page-height are
    # raised, so the true limits are the page geometry maxima.
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "221.121"
    assert command[command.index("-y") + 1] == "876.695"
    assert command.index("--page-height") < command.index("-y")


def test_build_scan_command_auto_size_enables_lower_edge_detection(
    tmp_path: Path,
) -> None:
    caps = {"ald": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    no_ald_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--ald=yes" in auto_command
    assert "--ald=yes" not in fixed_command
    assert "--ald=yes" not in no_ald_command


def test_build_scan_command_fixed_size_may_exceed_the_bare_axis_range(
    tmp_path: Path,
) -> None:
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="legal"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--page-height") + 1] == "355.6"
    assert command[command.index("-y") + 1] == "355.6"


def test_build_scan_command_auto_size_enables_hardware_adf_cropping(
    tmp_path: Path,
) -> None:
    # epsonds' "ADF auto cropping": white-backing devices cannot be cropped
    # in software, so auto page size hands the job to the hardware.
    caps = {"adf-crp": Capability(kind="bool"), "adf-skew": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4", deskew=True),
        "test:0",
        caps,
        str(tmp_path / "p_%04d.pnm"),
    )

    assert "--adf-crp=yes" in auto_command
    assert "--adf-crp=yes" not in fixed_command
    assert "--adf-skew=no" in auto_command
    assert "--adf-skew=yes" in fixed_command


def test_build_scan_command_degraded_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    # Flatbed-only device, duplex requested: the mapper degrades to the
    # flatbed, and --batch-count=1 must key on that mapped source or the
    # batch loops forever (a flatbed never reports "feeder empty").
    caps = {"source": Capability(kind="enum", choices=["Flatbed"])}

    command, effective = build_scan_command(
        _config(source="adf-duplex"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert effective.source == "Flatbed"
    assert command[command.index("--source") + 1] == "Flatbed"
    assert "--batch-count=1" in command


def test_build_scan_command_requested_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    command, _effective = build_scan_command(
        _config(source="flatbed"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-count=1" in command


def test_build_scan_command_auto_size_omits_geometry_without_ranges(
    tmp_path: Path,
) -> None:
    caps = {"x": Capability(kind="other")}

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert "-x" not in command
    assert "-y" not in command


def test_build_scan_command_reports_the_snapped_resolution(tmp_path: Path) -> None:
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}

    command, effective = build_scan_command(
        _config(resolution=300), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--resolution") + 1] == "150"
    assert effective.resolution == 150


def test_scan_to_files_returns_the_effective_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert result.settings.resolution == 150


def test_scan_to_files_reprobes_with_the_mapped_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF", "ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    probes: list[tuple[tuple[str, str], ...]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    # Bare, source-applied, then the final acquisition state (source plus
    # the negotiated options; no mode option exists here).
    assert probes == [
        (),
        (("--source", "ADF Duplex"),),
        (("--source", "ADF Duplex"),),
    ]


def test_scan_to_files_sweeps_pages_scanimage_did_not_announce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("page_0002.pnm", "page_0001.pnm"):
        (tmp_path / name).write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(), "test:0", tmp_path, EventWriter(enabled=False), seen.append
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert seen == result.pages


def test_scan_to_files_raises_when_nothing_was_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(), "test:0", tmp_path, EventWriter(enabled=False), lambda p: None
        )


def test_scan_to_files_reports_scan_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage",
        lambda command, on_page: (1, "scanimage: sane_start failed"),
    )

    with pytest.raises(DeviceError, match="sane_start failed"):
        scan_to_files(
            _config(), "test:0", tmp_path, EventWriter(enabled=False), lambda p: None
        )


def test_backend_deskew_marks_the_request_as_applied() -> None:
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "swdeskew": Capability(kind="bool"),
    }

    _, with_deskew = build_scan_command(
        _config(deskew=True), "dev", caps, "out/page_%04d.pnm"
    )
    _, without = build_scan_command(
        _config(deskew=False), "dev", caps, "out/page_%04d.pnm"
    )
    _, no_option = build_scan_command(
        _config(deskew=True),
        "dev",
        {"source": Capability(kind="enum", choices=["ADF Duplex"])},
        "out/page_%04d.pnm",
    )

    assert with_deskew.deskew_applied is True
    assert without.deskew_applied is False  # the option was set to =no
    assert no_option.deskew_applied is False  # nothing there to take the job


def _fixture_caps(name: str) -> dict[str, Capability]:
    from scanmole.options import parse_capabilities

    fixtures = Path(__file__).parent.parent / "fixtures" / "scanimage-A"
    return parse_capabilities((fixtures / name).read_text())


def test_scan_refuses_without_resolution_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --resolution option at all: the effective dpi stays UNKNOWN and
    # acquisition must fail before any paper is fed.
    caps = {"source": Capability(kind="enum", choices=["ADF Duplex"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="physical resolution"):
        scan_to_files(
            _config(), "test:0", tmp_path, EventWriter(enabled=False), lambda p: None
        )


def test_fixed_resolution_reaches_settings_without_being_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inactive option pinned to 200 dpi establishes the effective dpi:
    # nothing is emitted, but the settings and the settings event carry it.
    import io
    import json

    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="enum", choices=["200dpi"], active=False),
    }
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p: None,
    )

    assert "--resolution" not in commands[0]
    assert result.settings.resolution == 200
    settings_event = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "settings"
    )
    assert settings_event["resolution"] == 200


def test_source_dependent_snapshot_decides_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The authoritative source-applied listing advertises a smaller range
    # than the bare one; the emitted and reported dpi must follow it.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    bare = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    sourced = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=150),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        return sourced if settings else bare

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def _res(minimum: float, maximum: float) -> Capability:
    return Capability(kind="range", minimum=minimum, maximum=maximum)


def test_mode_dependent_resolution_is_renegotiated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 50..600 dpi in the bare and source listings, but only 50..150 once
    # Lineart is applied: the emitted and reported dpi must come from the
    # final acquisition state, not the earlier optimistic range.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        applied_mode = any(option == "--mode" for option, _value in settings)
        return {**base, "resolution": _res(50, 150 if applied_mode else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_software_faint_resolution_follows_the_gray_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The adaptive faint path scans Gray: the dpi must come from the probe
    # with Gray (and the pinned depth) applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        gray = ("--mode", "Gray") in settings
        return {**base, "resolution": _res(50, 150 if gray else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_native_faint_resolution_follows_the_enhanced_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native SDTC path applies Lineart plus its extras; the dpi must
    # come from the probe with that complete state applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
        if ("--mode", "Lineart") in settings:
            caps["resolution"] = _res(50, 150)
        return caps

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert commands[0][commands[0].index("--threshold") + 1] == "0"  # native path
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_faint_command_engages_fujitsu_sdtc_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    probes: list[tuple[tuple[str, str], ...]] = []
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return _fixture_caps("fujitsu-scansnap-ix500.txt")

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    source = ("--source", "ADF Duplex")
    final = (source, ("--mode", "Lineart"), ("--threshold", "0"), ("--variance", "0"))
    assert probes == [
        (),
        (source,),
        (source, ("--mode", "Lineart")),
        final,  # the SDTC verification reprobe
        final,  # the final-state probe deciding resolution and geometry
    ]
    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 6] == [
        "--mode",
        "Lineart",
        "--threshold",
        "0",
        "--variance",
        "0",
    ]
    assert "--depth" not in command
    assert result.settings.faint_native is True


def test_faint_command_engages_epson_tet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps(
            "epson-perfection1660-epson2.txt"
        ),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    scan_to_files(
        _config(lineart_threshold="auto", source="flatbed", page_size="a4"),
        "epson2:libusb:001:004",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 4] == [
        "--mode",
        "Lineart",
        "--halftoning",
        "Text Enhanced Technology",
    ]


def test_faint_fallback_scans_gray_with_pinned_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The epsonds DS-730N has no native enhancement: the faint scan must
    # acquire Gray at an explicit 8 bit, never the device's plain Lineart.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps("epson-ds730n-epsonds.txt"),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "epsonds:net:192.168.0.167",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    command = commands[0]
    assert command[command.index("--mode") + 1] == "Gray"
    assert command[command.index("--depth") + 1] == "8"
    assert result.settings.faint_native is False


def test_faint_on_a_lineart_only_device_fails_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart"]),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: caps,
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="ordinary B/W"):
        scan_to_files(
            _config(lineart_threshold="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p: None,
        )


def test_faint_candidate_probe_failure_falls_back_to_gray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bare and source-applied probes succeed; the candidate-mode probe
    # raises. The scan must fall back to the software path, not abort.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        if ("--mode", "Lineart") in settings:  # only the candidate probes
            raise DeviceError("timed out probing options")
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert result.settings.faint_native is False


def test_scan_to_files_warns_exactly_once_per_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Negotiation runs twice (initial probe, source-applied reprobe) but the
    # selected plan's notices must reach the user exactly once.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with caplog.at_level("INFO"):
        scan_to_files(
            _config(),  # requests adf-duplex; only a front side exists
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p: None,
        )

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= 30 and "backs will not be scanned" in r.message
    ]
    assert len(warnings) == 1
