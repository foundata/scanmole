"""Tests for sheet-flow bookkeeping and the collect wait state machine."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from scanmole.events import EventWriter
from scanmole.sensors import SensorSnapshot
from scanmole.sheetflow import (
    CollectCommands,
    CollectController,
    PageOrigin,
    count_sheets,
    next_page_number,
)


def test_sheet_key_pairs_duplex_frames_within_a_segment() -> None:
    assert PageOrigin(segment=1, frame=1).sheet_key(duplex=True) == (1, 1)
    assert PageOrigin(segment=1, frame=2).sheet_key(duplex=True) == (1, 1)
    assert PageOrigin(segment=1, frame=3).sheet_key(duplex=True) == (1, 2)


def test_sheet_key_never_pairs_across_segments() -> None:
    # An odd final frame is an incomplete sheet; the next segment's first
    # frame starts a new physical sheet even though the global numbering
    # continues without a gap.
    last_of_first = PageOrigin(segment=1, frame=3).sheet_key(duplex=True)
    first_of_second = PageOrigin(segment=2, frame=1).sheet_key(duplex=True)

    assert last_of_first != first_of_second


def test_sheet_key_counts_each_simplex_frame_as_a_sheet() -> None:
    assert PageOrigin(segment=2, frame=3).sheet_key(duplex=False) == (2, 3)


def test_count_sheets_simplex_counts_frames() -> None:
    assert count_sheets([2, 3], duplex=False) == 5


def test_count_sheets_duplex_pairs_per_segment() -> None:
    # 3 frames in one segment are two physical sheets (one incomplete);
    # a global pairing over 3+2 frames would report wrongly.
    assert count_sheets([3, 2], duplex=True) == 3
    assert count_sheets([2, 2], duplex=True) == 2
    assert count_sheets([], duplex=True) == 0


def test_next_page_number_starts_at_one(tmp_path: Path) -> None:
    assert next_page_number(tmp_path) == 1


def test_next_page_number_follows_the_greatest_artifact(tmp_path: Path) -> None:
    # The greatest existing file decides, never the page-event count: an
    # unannounced or partial frame on disk must not be overwritten by the
    # next segment.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    (tmp_path / "page_0003.pnm").write_bytes(b"partial")
    (tmp_path / "unrelated.txt").write_text("x")

    assert next_page_number(tmp_path) == 4


def test_commands_retain_at_most_one_pending_next() -> None:
    commands = CollectCommands()
    commands.feed_line("next\n")
    commands.feed_line("next\n")

    assert commands.take_next()
    assert not commands.take_next()


def test_commands_done_is_sticky_and_idempotent() -> None:
    commands = CollectCommands()
    commands.feed_line("done\n")
    commands.feed_line("done\n")

    assert commands.done_pending()
    assert commands.done_pending()


def test_commands_ignore_blank_lines() -> None:
    commands = CollectCommands()
    commands.feed_line("\n")
    commands.feed_line("   \n")

    assert not commands.done_pending()
    assert not commands.take_next()


def test_commands_warn_on_unknown_input_without_effect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    commands = CollectCommands()

    with caplog.at_level(logging.WARNING):
        commands.feed_line("frobnicate\n")

    assert "frobnicate" in caplog.text
    assert not commands.done_pending()
    assert not commands.take_next()


def test_commands_eof_counts_as_done() -> None:
    commands = CollectCommands()
    commands.feed_eof()

    assert commands.done_pending()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SensorScript:
    """Scripted sensor reads; the last snapshot repeats forever."""

    def __init__(self, *snapshots: SensorSnapshot) -> None:
        self.queue = list(snapshots)
        self.reads = 0

    def __call__(self) -> SensorSnapshot:
        self.reads += 1
        if len(self.queue) > 1:
            return self.queue.pop(0)
        return self.queue[0]


def _controller(
    *,
    sensors: _SensorScript,
    commands: CollectCommands | None = None,
    feeder: bool = True,
    duplex: bool = False,
    idle_seconds: float = 900.0,
) -> tuple[CollectController, CollectCommands, _Clock, io.StringIO]:
    commands = commands if commands is not None else CollectCommands()
    clock = _Clock()
    stream = io.StringIO()

    def wait(seconds: float) -> None:
        clock.advance(seconds)

    controller = CollectController(
        commands=commands,
        read_sensors=sensors,
        feeder=feeder,
        duplex=duplex,
        events=EventWriter(enabled=True, stream=stream),
        clock=clock,
        wait=wait,
        idle_seconds=idle_seconds,
    )
    return controller, commands, clock, stream


def _events(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


_PAPER = SensorSnapshot(scan=False, page_loaded=True)
_NO_PAPER = SensorSnapshot(scan=False, page_loaded=False)
_NO_SENSORS = SensorSnapshot()


def _recording_acquire(acquired: list[int]) -> Callable[[], int]:
    def acquire() -> int:
        acquired.append(1)
        return 1

    return acquire


def test_paper_present_at_entry_starts_without_a_waiting_event() -> None:
    # Also pins the boundary priority: after the segment the sheet is
    # still loaded, but the done fed during acquisition wins over the
    # sensor readiness, so exactly one segment runs.
    sensors = _SensorScript(_PAPER)
    controller, commands, _clock, stream = _controller(sensors=sensors)
    segments: list[int] = []

    def acquire() -> int:
        segments.append(1)
        commands.feed_line("done\n")
        return 2

    controller.run(acquire)

    assert segments == [1]
    assert controller.segment_frames == (2,)
    assert _events(stream) == []  # started immediately: nothing to wait for


def test_empty_sensor_feeder_never_starts_and_times_out() -> None:
    sensors = _SensorScript(_NO_PAPER)
    controller, _commands, clock, stream = _controller(
        sensors=sensors, idle_seconds=10.0
    )
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == []
    assert clock.now >= 10.0
    waiting = _events(stream)
    assert len(waiting) == 1
    assert waiting[0] == {
        "event": "waiting",
        "sheets": 0,
        "pages": 0,
        "idle_seconds": 10,
        "manual_trigger": False,
    }


def test_paper_insertion_continues_the_collection() -> None:
    # Segment 1 starts on loaded paper; the second wait sees an empty
    # slot, then paper arrives on a later poll and segment 2 runs.
    sensors = _SensorScript(_PAPER, _NO_PAPER, _NO_PAPER, _PAPER)
    controller, commands, _clock, stream = _controller(sensors=sensors, duplex=True)
    calls: list[int] = []

    def acquire() -> int:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            commands.feed_line("done\n")
        return 3 if len(calls) == 1 else 2

    controller.run(acquire)

    assert calls == [1, 2]
    events = _events(stream)
    assert len(events) == 1  # only the second segment actually waited
    # Segment-aware duplex counting: 3 frames are 2 physical sheets.
    assert events[0]["sheets"] == 2
    assert events[0]["pages"] == 3
    assert events[0]["manual_trigger"] is False


def test_a_button_press_without_paper_never_starts_an_empty_scan() -> None:
    sensors = _SensorScript(SensorSnapshot(scan=True, page_loaded=False))
    controller, _commands, _clock, _stream = _controller(
        sensors=sensors, idle_seconds=5.0
    )
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == []


def test_a_stale_button_latch_at_entry_is_discarded() -> None:
    # The baseline read consumes a press made during the acquisition; a
    # backend that keeps reporting yes afterwards must not retrigger.
    sensors = _SensorScript(SensorSnapshot(scan=True, page_loaded=None))
    controller, _commands, _clock, stream = _controller(
        sensors=sensors, feeder=False, idle_seconds=5.0
    )
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    # The launching action scans one sheet; the button that stayed pressed
    # through it is baseline state at the next wait, never a second trigger.
    assert acquired == [1]
    assert _events(stream)[0]["manual_trigger"] is True


def test_a_fresh_button_edge_starts_the_next_frame() -> None:
    sensors = _SensorScript(
        SensorSnapshot(scan=False, page_loaded=None),
        SensorSnapshot(scan=False, page_loaded=None),
        SensorSnapshot(scan=True, page_loaded=None),
    )
    controller, commands, _clock, _stream = _controller(
        sensors=sensors, feeder=False, idle_seconds=60.0
    )
    calls: list[int] = []

    def acquire() -> int:
        calls.append(1)
        commands.feed_line("done\n")
        return 1

    controller.run(acquire)

    assert calls == [1]


def test_sensorless_wait_requires_next_and_never_polls() -> None:
    # Without usable sensors the wait must not probe the device blindly:
    # one baseline read per wait entry, none inside the loop. The first
    # segment runs on the launching action, so the wait under test is the
    # one after it, and no further next ever arrives.
    sensors = _SensorScript(_NO_SENSORS)
    controller, _commands, _clock, stream = _controller(
        sensors=sensors, feeder=True, idle_seconds=60.0
    )
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == [1]  # the launching action, and nothing after it
    assert sensors.reads == 2  # one baseline per decision, none in the loop
    assert _events(stream)[0]["manual_trigger"] is True


def test_a_pending_next_starts_a_sensorless_feeder_immediately() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, commands, _clock, stream = _controller(
        sensors=sensors, feeder=True, idle_seconds=60.0
    )
    commands.feed_line("next\n")
    calls: list[int] = []

    def acquire() -> int:
        calls.append(1)
        commands.feed_line("done\n")
        return 1

    controller.run(acquire)

    assert calls == [1]
    assert _events(stream) == []  # the pending next made waiting unnecessary


def test_next_cannot_bypass_the_paper_precondition() -> None:
    # A usable paper sensor governs the feeder: a manual next with an
    # empty slot is consumed without starting an empty acquisition.
    sensors = _SensorScript(_NO_PAPER)
    controller, commands, _clock, _stream = _controller(
        sensors=sensors, idle_seconds=5.0
    )
    commands.feed_line("next\n")
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == []


def test_done_wins_over_a_simultaneous_next() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, commands, _clock, _stream = _controller(sensors=sensors, feeder=False)
    commands.feed_line("next\n")
    commands.feed_line("done\n")
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == []


def test_idle_timeout_ends_with_a_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensors = _SensorScript(_NO_PAPER)
    controller, _commands, _clock, _stream = _controller(
        sensors=sensors, idle_seconds=10.0
    )

    with caplog.at_level(logging.INFO):
        controller.run(lambda: 1)

    assert "idle timeout" in caplog.text


def test_idle_timeout_resets_after_each_segment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Segment 1 starts after 8 s of a 10 s window; the second wait then
    # runs 8 s more (16 s total, past the original deadline) before its
    # trigger. A deadline that failed to reset would expire instead.
    sensors = _SensorScript(
        _NO_PAPER,  # baseline of wait 1
        _NO_PAPER,  # polls of wait 1 ...
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _PAPER,  # paper arrives at ~8 s
        _NO_PAPER,  # baseline of wait 2
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _NO_PAPER,
        _PAPER,  # arrives again at ~8 s into wait 2
    )
    controller, commands, _clock, _stream = _controller(
        sensors=sensors, idle_seconds=10.0
    )
    calls: list[int] = []

    def acquire() -> int:
        calls.append(1)
        if len(calls) == 2:
            commands.feed_line("done\n")
        return 1

    with caplog.at_level(logging.INFO):
        controller.run(acquire)

    assert calls == [1, 1]
    assert "idle timeout" not in caplog.text


def test_an_interrupt_during_the_wait_propagates() -> None:
    sensors = _SensorScript(_NO_PAPER)
    commands = CollectCommands()
    clock = _Clock()

    def interrupting_wait(seconds: float) -> None:
        raise KeyboardInterrupt

    controller = CollectController(
        commands=commands,
        read_sensors=sensors,
        feeder=True,
        duplex=False,
        events=EventWriter(enabled=False),
        clock=clock,
        wait=interrupting_wait,
        idle_seconds=60.0,
    )

    with pytest.raises(KeyboardInterrupt):
        controller.run(lambda: 1)


def test_acquisition_failures_propagate_unchanged() -> None:
    sensors = _SensorScript(_PAPER)
    controller, _commands, _clock, _stream = _controller(sensors=sensors)
    error = RuntimeError("backend died")

    def acquire() -> int:
        raise error

    with pytest.raises(RuntimeError) as info:
        controller.run(acquire)

    assert info.value is error


# ---- the launching action is the first trigger ---------------------------


def _finishing_acquire(
    commands: CollectCommands, acquired: list[int]
) -> Callable[[], int]:
    """Acquire once, then finish, so a first-trigger test cannot loop."""

    def acquire() -> int:
        acquired.append(1)
        commands.feed_line("done\n")
        return 1

    return acquire


def test_a_sensorless_feeder_scans_the_first_sheet_without_waiting() -> None:
    # Pressing Scan is the trigger. Requiring Next Sheet before page one
    # would make the first action do nothing visible.
    sensors = _SensorScript(_NO_SENSORS)
    controller, commands, _clock, stream = _controller(sensors=sensors, feeder=True)
    acquired: list[int] = []

    controller.run(_finishing_acquire(commands, acquired))

    assert acquired == [1]
    assert _events(stream) == []  # nothing was waited for, so no event


def test_a_flatbed_scans_the_first_sheet_without_waiting() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, commands, _clock, stream = _controller(sensors=sensors, feeder=False)
    acquired: list[int] = []

    controller.run(_finishing_acquire(commands, acquired))

    assert acquired == [1]
    assert _events(stream) == []


def test_a_button_only_source_scans_the_first_sheet_without_waiting() -> None:
    # The button press that started the collection counts; a second press
    # must not be required before anything is scanned.
    sensors = _SensorScript(SensorSnapshot(scan=False, page_loaded=None))
    controller, commands, _clock, stream = _controller(sensors=sensors, feeder=True)
    acquired: list[int] = []

    controller.run(_finishing_acquire(commands, acquired))

    assert acquired == [1]
    assert _events(stream) == []


def test_a_pending_done_still_finishes_before_the_first_acquisition() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, commands, _clock, stream = _controller(sensors=sensors, feeder=True)
    commands.feed_line("done\n")
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == []
    assert _events(stream) == []


def test_a_sensed_feeder_still_governs_the_first_acquisition() -> None:
    # A usable paper level outranks the launching action in both
    # directions: present starts, absent waits rather than running an
    # empty feeder.
    present = _SensorScript(SensorSnapshot(scan=False, page_loaded=True))
    controller, commands, _clock, stream = _controller(sensors=present, feeder=True)
    acquired: list[int] = []
    controller.run(_finishing_acquire(commands, acquired))
    assert acquired == [1]
    assert _events(stream) == []

    absent = _SensorScript(SensorSnapshot(scan=False, page_loaded=False))
    controller, _commands, _clock, stream = _controller(
        sensors=absent, feeder=True, idle_seconds=60.0
    )
    acquired = []
    controller.run(_recording_acquire(acquired))
    assert acquired == []  # never start an empty sensed feeder
    assert _events(stream)[0]["manual_trigger"] is False


def test_a_failure_during_the_first_acquisition_propagates() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, _commands, _clock, stream = _controller(sensors=sensors, feeder=True)

    def failing() -> int:
        raise RuntimeError("lamp failure")

    with pytest.raises(RuntimeError, match="lamp failure"):
        controller.run(failing)
    assert _events(stream) == []  # it failed acquiring, not waiting


def test_the_second_sheet_still_waits_for_an_explicit_trigger() -> None:
    # Only the first segment is exempt; afterwards the ordinary manual
    # wait applies and its waiting event is emitted.
    sensors = _SensorScript(
        SensorSnapshot(scan=False, page_loaded=None),  # baseline, segment one
        SensorSnapshot(scan=False, page_loaded=None),  # baseline of the wait
        SensorSnapshot(scan=True, page_loaded=None),  # the press that resumes
    )
    controller, commands, _clock, stream = _controller(
        sensors=sensors, feeder=True, idle_seconds=60.0
    )
    acquired: list[int] = []

    def acquire() -> int:
        acquired.append(1)
        if len(acquired) == 2:
            commands.feed_line("done\n")
        return 1

    controller.run(acquire)

    assert acquired == [1, 1]
    events = _events(stream)
    assert len(events) == 1  # one genuine wait, between the two segments
    assert events[0]["manual_trigger"] is True


def test_idle_expiry_after_the_first_segment_still_ends_the_run() -> None:
    sensors = _SensorScript(_NO_SENSORS)
    controller, _commands, _clock, stream = _controller(
        sensors=sensors, feeder=True, idle_seconds=5.0
    )
    acquired: list[int] = []

    controller.run(_recording_acquire(acquired))

    assert acquired == [1]  # the launching action, then nothing more arrived
    assert len(_events(stream)) == 1
