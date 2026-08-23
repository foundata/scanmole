"""Sheet-flow bookkeeping and the collect wait loop, GTK-free.

A collect run invokes the scanner once per feeder reload (one *acquisition
segment*) while the page files keep one continuous numbering. Global page
numbers therefore cannot say where a physical sheet starts: a duplex segment
that ended on an odd frame left an incomplete sheet, and the next segment's
first frame starts a new sheet regardless of the numbering. The segment and
the frame's position within it travel with every delivered page instead of
being re-derived later.

Between segments the :class:`CollectController` waits for the next trigger:
paper presence on a feeder with a usable sensor, a fresh scan-button edge,
or a manual ``next`` on standard input; ``done`` (or end of input) finishes
the collection. The first sheet is the exception: without a usable paper
level there is nothing to consult, so the action that started the
collection is its own trigger and the first segment runs immediately.
Waiting never starts the scanner blindly, and it is bounded by one
documented idle timeout.
"""

from __future__ import annotations

import enum
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from scanmole.events import EventWriter
from scanmole.sensors import SensorSnapshot

LOGGER = logging.getLogger(__name__)

_PAGE_FILE = re.compile(r"page_(\d+)\.pnm")

COLLECT_IDLE_TIMEOUT_SECONDS = 900.0
"""How long a collect run waits for the next sheet before ending itself.

15 minutes: generous enough for fetching more paper, bounded enough that
an abandoned session cannot wait forever. It applies only while waiting,
resets after every completed segment, and is separate from the
per-invocation scan timeout. Expiry finalizes normally when pages exist
and raises the ordinary no-pages error when nothing was scanned."""

COLLECT_POLL_SECONDS = 1.0
"""Cadence of the sensor polling inside a collect wait.

Measured on hardware: one sensor read costs about 0.3 s, a blind
scanimage start against an empty feeder about 2 s, and a single button
press latches until the next read at this cadence, so polling at about
1 Hz sees every press without churning the device."""


@dataclass(frozen=True)
class PageOrigin:
    """Where a delivered frame came from, both indices 1-based."""

    segment: int
    """The acquisition segment (scanner invocation) that produced the frame."""
    frame: int
    """The frame's position within its segment."""

    def sheet_key(self, duplex: bool) -> tuple[int, int]:
        """Group key of the physical sheet this frame belongs to.

        On a duplex source frames 1 and 2 of a segment are one sheet,
        frames 3 and 4 the next; elsewhere every frame is its own sheet.
        The segment is part of the key, so an odd final frame can never
        pair with the first frame of the following segment.
        """
        return (self.segment, (self.frame + 1) // 2 if duplex else self.frame)


def count_sheets(segment_frames: Sequence[int], duplex: bool) -> int:
    """Physical sheets represented by per-segment frame counts.

    A duplex segment with an odd frame count still counts its incomplete
    final sheet: the paper went through the scanner.
    """
    if not duplex:
        return sum(segment_frames)
    return sum((frames + 1) // 2 for frames in segment_frames)


class CollectCommands:
    """Thread-safe manual-control state for a collect run.

    Fed line by line from a reader (normally standard input). ``done`` is
    sticky and idempotent; at most one pending ``next`` is retained; blank
    lines are ignored; unknown commands warn once per line and change
    nothing. End of input counts as ``done``: no further manual commands
    can arrive, so a finish that cannot come must not be waited for.
    """

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._next = False
        self._done = False

    def feed_line(self, raw: str) -> None:
        """Accept one newline-terminated command line."""
        text = raw.strip()
        if not text:
            return
        if text not in ("next", "done"):
            LOGGER.warning("ignoring unknown collect command: %s", text)
            return
        with self._changed:
            if text == "done":
                self._done = True
            else:
                self._next = True
            self._changed.notify_all()

    def feed_eof(self) -> None:
        """Record the end of the command stream (treated as ``done``)."""
        with self._changed:
            self._done = True
            self._changed.notify_all()

    def done_pending(self) -> bool:
        """Whether the collection should finish at the next boundary."""
        with self._changed:
            return self._done

    def take_next(self) -> bool:
        """Consume the pending ``next`` request, if one is retained."""
        with self._changed:
            pending, self._next = self._next, False
            return pending

    def wait(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early when new input arrives."""
        with self._changed:
            if self._done or self._next:
                return
            self._changed.wait(seconds)


def start_stdin_commands(stream: TextIO) -> CollectCommands:
    """Read collect commands from ``stream`` on a background thread.

    The thread is a daemon on purpose: a reader blocked on an interactive
    terminal cannot be joined at process end, and it owns no state that
    needs unwinding. A closed pipe or stream teardown counts as end of
    input.
    """
    commands = CollectCommands()

    def pump() -> None:
        try:
            for raw in stream:
                commands.feed_line(raw)
        except (OSError, ValueError):
            LOGGER.debug("collect command stream ended abnormally", exc_info=True)
        commands.feed_eof()

    threading.Thread(target=pump, name="scanmole-collect-stdin", daemon=True).start()
    return commands


class Trigger(enum.Enum):
    """Why a collect wait ended."""

    START = "start"
    DONE = "done"
    IDLE = "idle"


class CollectController:
    """The wait loop between collect acquisition segments.

    States: acquire the first segment, wait for the next trigger, acquire,
    ... until ``done`` or the idle timeout. Every wait entry performs one
    baseline sensor read whose button latch is deliberately discarded (a
    press made during the acquisition must not trigger another segment)
    while its paper level is honored. The scanner is never started against
    an empty feeder whose paper sensor is usable, and devices without
    usable sensors are never polled blindly: after the first segment they
    wait for an explicit ``next`` or a fresh button edge.

    The first segment is decided differently, because there has been no
    chance to wait for anything yet. A pending ``done`` still wins. A
    usable paper level still governs, so an empty sensed feeder waits
    rather than starting. Everything else acquires at once: the click,
    command or button press that started the collection is the trigger,
    and demanding a second one before page one would be a surprise.
    """

    def __init__(
        self,
        *,
        commands: CollectCommands,
        read_sensors: Callable[[], SensorSnapshot],
        feeder: bool,
        duplex: bool,
        events: EventWriter,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] | None = None,
        idle_seconds: float = COLLECT_IDLE_TIMEOUT_SECONDS,
        poll_seconds: float = COLLECT_POLL_SECONDS,
    ) -> None:
        """Wire the controller's seams (clock, wait and input injectable)."""
        self._commands = commands
        self._read_sensors = read_sensors
        self._feeder = feeder
        self._duplex = duplex
        self._events = events
        self._clock = clock
        self._wait = wait if wait is not None else commands.wait
        self._idle_seconds = idle_seconds
        self._poll_seconds = poll_seconds
        self._segments: list[int] = []

    @property
    def segment_frames(self) -> tuple[int, ...]:
        """Frames acquired per completed segment, in order."""
        return tuple(self._segments)

    def run(self, acquire: Callable[[], int]) -> None:
        """Alternate waiting and acquiring until done or idle expiry.

        ``acquire`` runs one scanner invocation and returns the number of
        frames it delivered; its failures propagate unchanged, so the
        established abort-and-preserve contract applies.
        """
        first = True
        while True:
            trigger = self._await_trigger(first)
            if trigger is Trigger.DONE:
                return
            if trigger is Trigger.IDLE:
                LOGGER.info(
                    "collection ended after the idle timeout (%d s without "
                    "a new sheet)",
                    int(self._idle_seconds),
                )
                return
            self._segments.append(acquire())
            first = False

    def _await_trigger(self, first: bool = False) -> Trigger:
        """Decide whether to acquire, finish or wait.

        ``first`` marks the decision before any segment has run, where the
        launching action stands in for a trigger on sensorless sources.
        """
        baseline = self._read_sensors()
        auto = self._feeder and baseline.page_loaded is not None
        watch_button = not auto and baseline.scan is not None
        last_button = baseline.scan  # the baseline latch never triggers
        # done wins over next and sensor readiness at every boundary.
        if self._commands.done_pending():
            return Trigger.DONE
        if auto and baseline.page_loaded:
            return Trigger.START
        if first and not auto:
            # Without a paper level to consult, the action that started the
            # collection (the Scan click, the CLI invocation, a button press
            # mapped to collect) is itself the trigger for the first sheet.
            # Waiting here would demand a second action before page one, and
            # nothing is waited for, so no waiting event is emitted.
            return Trigger.START
        if self._commands.take_next() and not auto:
            # With a usable paper sensor, paper presence governs; a manual
            # next cannot bypass the empty-feeder precondition and is
            # consumed above without effect.
            return Trigger.START
        self._emit_waiting(manual=not auto)
        deadline = self._clock() + self._idle_seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return Trigger.IDLE
            self._wait(min(self._poll_seconds, remaining))
            if self._commands.done_pending():
                return Trigger.DONE
            if self._commands.take_next() and not auto:
                return Trigger.START
            if self._clock() >= deadline:
                return Trigger.IDLE
            if not (auto or watch_button):
                continue  # no usable sensors: never poll the device blindly
            snapshot = self._read_sensors()
            if auto and snapshot.page_loaded:
                return Trigger.START
            if watch_button and snapshot.scan is not None:
                # One trigger per fresh no-to-yes edge, even if a backend
                # keeps the value latched across reads.
                fresh = snapshot.scan and last_button is not True
                last_button = snapshot.scan
                if fresh:
                    return Trigger.START

    def _emit_waiting(self, manual: bool) -> None:
        sheets = count_sheets(self._segments, self._duplex)
        pages = sum(self._segments)
        self._events.emit(
            "waiting",
            sheets=sheets,
            pages=pages,
            idle_seconds=int(self._idle_seconds),
            manual_trigger=manual,
        )
        if manual:
            LOGGER.info(
                "Waiting for the next sheet: place it and enter 'next' "
                "(or press the scanner's button); enter 'done' to finish."
            )
        else:
            LOGGER.info(
                "Waiting for the next sheet: insert it to continue, or "
                "enter 'done' to finish."
            )


def page_file_number(path: Path) -> int | None:
    """The number in a ``page_NNNN.pnm`` artifact name, or ``None``."""
    match = _PAGE_FILE.fullmatch(path.name)
    return int(match.group(1)) if match else None


def next_page_number(work_dir: Path) -> int:
    """1 + the greatest existing page artifact number (1 when none exist).

    Numbering scans the directory rather than counting page events, so an
    unannounced or partial frame on disk is never overwritten by the next
    segment.
    """
    greatest = 0
    for path in work_dir.iterdir():
        number = page_file_number(path)
        if number is not None:
            greatest = max(greatest, number)
    return greatest + 1
