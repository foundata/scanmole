"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.scanner import (
    _config,
)

from scanmole.errors import NoPagesError
from scanmole.events import EventWriter
from scanmole.options import (
    Capability,
)
from scanmole.scanner import (
    scan_to_files,
)
from scanmole.sensors import SensorSnapshot
from scanmole.sheetflow import CollectCommands, PageOrigin


def _sensor_caps() -> dict[str, Capability]:
    return {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }


def _collect_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paper: bool = True,
) -> tuple[CollectCommands, list[list[str]]]:
    """Wire a collect run: fake probes, fake sensors, owned command channel."""
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): _sensor_caps(),
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=False, page_loaded=paper),
    )
    calls: list[list[str]] = []
    return commands, calls


def test_collect_runs_segments_across_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        start = 1
        for part in cmd:
            if part.startswith("--batch-start="):
                start = int(part.split("=")[1])
        page = tmp_path / f"page_{start:04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        if len(calls) == 2:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    origins: list[PageOrigin] = []

    result = scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: origins.append(o),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert not any(part.startswith("--batch-start") for part in calls[0])
    assert "--batch-start=2" in calls[1]
    assert origins == [
        PageOrigin(segment=1, frame=1),
        PageOrigin(segment=2, frame=1),
    ]


def test_collect_emits_settings_once_and_reads_sensors_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): _sensor_caps(),
    )
    sensor_settings: list[tuple[tuple[str, str], ...]] = []

    def fake_sensors(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> SensorSnapshot:
        sensor_settings.append(tuple(settings))
        return SensorSnapshot(scan=False, page_loaded=True)

    monkeypatch.setattr("scanmole.scanner.probe_sensors", fake_sensors)
    runs: list[int] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        runs.append(1)
        page = tmp_path / f"page_{len(runs):04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        if len(runs) == 2:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert events.count("settings") == 1
    # Every sensor read applied the final acquisition settings, so the
    # snapshot is the same option-dependent state the scan will use.
    assert sensor_settings
    assert set(sensor_settings) == {
        (("--source", "ADF Front"), ("--resolution", "300"))
    }


def test_collect_counts_sheets_the_way_the_pipeline_pairs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a conclusively duplex source the waiting status and the pipeline
    # must agree: two frames of one physical sheet are one sheet.
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "source": Capability(kind="enum", choices=["ADF Duplex"]),
            "resolution": Capability(kind="range", minimum=50, maximum=600),
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=None, page_loaded=None),
    )

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        for index in (1, 2):  # front and back of one physical sheet
            page = tmp_path / f"page_{index:04d}.pnm"
            page.write_bytes(b"P4\n1 1\n\x00")
            on_page(page)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert result.settings.duplex is True
    waiting = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "waiting"
    ]
    assert [(event["sheets"], event["pages"]) for event in waiting] == [(1, 2)]


def test_collect_numbers_the_next_segment_past_unannounced_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        if len(calls) == 1:
            announced = tmp_path / "page_0001.pnm"
            announced.write_bytes(b"P4\n1 1\n\x00")
            on_page(announced)
            # A completed frame the announcement race missed.
            (tmp_path / "page_0002.pnm").write_bytes(b"P4\n1 1\n\x00")
        else:
            commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    seen: list[tuple[str, PageOrigin]] = []

    scan_to_files(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append((p.name, o)),
    )

    # The sweep delivered the unannounced frame on its own segment and the
    # next invocation numbered past it instead of overwriting it.
    assert ("page_0002.pnm", PageOrigin(segment=1, frame=2)) in seen
    assert "--batch-start=3" in calls[1]


def test_collect_stops_when_a_swept_frame_fails_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A swept frame goes through the same callback as an announced one, so
    # a failure there must stop the collection and reach the ordinary
    # preservation path, never be skipped or replaced.
    _commands, calls = _collect_setup(tmp_path, monkeypatch)

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        calls.append(cmd)
        (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    def failing_processing(page: Path, origin: PageOrigin) -> None:
        raise ValueError("page processing failed")

    with pytest.raises(ValueError, match="processing failed"):
        scan_to_files(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            failing_processing,
        )

    assert len(calls) == 1  # no further segment was started
    assert (tmp_path / "page_0001.pnm").exists()  # left exactly as written


def test_collect_idle_timeout_without_pages_raises_no_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commands, _calls = _collect_setup(tmp_path, monkeypatch, paper=False)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)

    def never_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        raise AssertionError("scanimage must not start against an empty feeder")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_collect_scans_the_first_sheet_on_a_sensorless_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end through the engine: a device without usable sensors must
    # produce page one from the run that started it, with no waiting event
    # before it and no second command needed.
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "source": Capability(kind="enum", choices=["Flatbed"]),
            "resolution": Capability(kind="range", minimum=50, maximum=600),
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=None, page_loaded=None),
    )
    runs: list[int] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        runs.append(1)
        page = tmp_path / f"page_{len(runs):04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        commands.feed_line("done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(source="flatbed", sheet_flow="collect"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert len(result.pages) == 1
    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert "waiting" not in events  # the scan start was the trigger


def test_an_unknown_source_collect_run_bounds_every_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # End to end through the engine: an unreadable listing bounds the
    # first acquisition and every reload, warns once on stderr, and leaves
    # the JSON event stream exactly as it was.
    commands = CollectCommands()
    monkeypatch.setattr("scanmole.scanner._collect_commands", lambda: commands)
    monkeypatch.setattr("scanmole.scanner.COLLECT_IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_sensors",
        lambda device, settings=(): SensorSnapshot(scan=None, page_loaded=None),
    )
    issued: list[list[str]] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        issued.append(cmd)
        page = tmp_path / f"page_{len(issued):04d}.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        commands.feed_line("next\n" if len(issued) == 1 else "done\n")
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        result = scan_to_files(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            tmp_path,
            EventWriter(enabled=True, stream=stream),
            lambda p, o: None,
        )

    assert len(issued) == 2
    for command in issued:
        assert "--batch-count=1" in command
    assert len(result.pages) == 2
    bounded = [
        record.getMessage()
        for record in caplog.records
        if "source capabilities" in record.getMessage()
    ]
    assert len(bounded) == 1  # the segment rebuild stays quiet

    # Both triggers were already pending, so nothing waited; the stream is
    # the same shape it has always been and carries no new field.
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["settings"]
    assert set(events[0]) == {"event", "device", "source", "mode", "resolution"}
