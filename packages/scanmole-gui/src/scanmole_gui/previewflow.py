"""The filename preview's lifecycle: when to look, and what may still render.

One look at the output folder is cheap; the hard part is deciding which
looks are worth taking and which of their results still describe what the
user is seeing. That is all this owns: the debounce that collapses a burst
of changes into one look, the generation that invalidates whatever is in
flight the moment an input moves, the single worker thread and the pending
rerun behind it, and the directory monitor that notices another process
writing into the selected folder.

Nothing here creates or reserves a name (see :mod:`scanmole_gui.preview`
for why), and nothing here touches a widget: the window passes in the
inputs and takes the finished line back through ``render``, so a look that
outlives the window it was started for renders nowhere.

This is local filesystem work and deliberately stays outside
:mod:`scanmole_gui.advisory`, which exists to cancel scanner access.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gio, GLib

from scanmole_gui.preview import PreviewOutcome, preview_outcome, preview_text

LOGGER = logging.getLogger(__name__)

DEBOUNCE_MS = 300
"""How long preview refresh requests coalesce before the folder is read.

Atomic replacements and temporary files make a directory monitor emit
several events for one logical change, and typing a template emits one
per keystroke. Both collapse into a single look at the folder."""


@dataclass(frozen=True)
class PreviewInputs:
    """Everything one look needs, read from the form at request time.

    ``folder`` is already expanded, because both the monitor and the
    candidate walk need the real directory rather than a ``~`` the user
    typed.
    """

    folder: Path
    template: str
    device: str | None


class PreviewFlow:
    """Owns the debounce, the worker, the generation and the monitor.

    ``alive`` is asked before anything is started or rendered: a window
    that is closing or has released its advisory work is not a window a
    preview may still write to. ``inputs`` is only ever called while it
    says yes, so the form is never read through a dead widget.
    """

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        inputs: Callable[[], PreviewInputs],
        render: Callable[[str], None],
    ) -> None:
        """Wire the flow to its window without holding a reference to it."""
        self._alive = alive
        self._inputs = inputs
        self._render = render
        self._generation = 0
        self._busy = False
        self._again = False
        self._debounce_id: int | None = None
        self._monitor: Gio.FileMonitor | None = None
        self._watched: Path | None = None

    def request(self) -> None:
        """Note that the inputs changed and coalesce a burst into one look.

        The generation advances here, not when a worker eventually starts:
        the moment the folder, template or device changes, whatever a
        running worker is about to report describes something else and
        must not reach the row. The debounce then collapses the burst, so
        an atomic replacement or a run of keystrokes costs one look.
        """
        if not self._alive():
            return
        self._generation += 1
        # Following the selected folder is part of asking: a monitor only
        # reports on the directory it was created for.
        self._watch_folder(self._inputs().folder)
        if self._debounce_id is not None:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(DEBOUNCE_MS, self._debounce_fired)

    def suspend(self) -> None:
        """Stop watching while hidden; a later request starts again."""
        self._stop_monitor()
        if self._debounce_id is not None:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = None
        # Anything in flight now describes a window nobody is looking at.
        self._generation += 1
        self._again = False

    def stop(self) -> None:
        """Teardown: drop the monitor, the debounce and any pending rerun."""
        self.suspend()

    # ------------------------------------------------------- the one look

    def _debounce_fired(self) -> bool:
        """Start the coalesced look once the burst has settled."""
        self._debounce_id = None
        self._start()
        return bool(GLib.SOURCE_REMOVE)

    def _start(self) -> None:
        """Look at the output folder off the main thread, once at a time."""
        if not self._alive():
            return
        if self._busy:
            # One worker at a time: this run's inputs are already the
            # newest, so a single rerun after the current one suffices
            # however many changes arrive meanwhile.
            self._again = True
            return
        generation = self._generation
        inputs = self._inputs()
        template = str(inputs.folder / inputs.template)
        device = inputs.device
        self._busy = True

        def work() -> None:
            outcome = preview_outcome(template, device)
            GLib.idle_add(self._apply, generation, outcome)

        threading.Thread(target=work, name="scanmole-preview", daemon=True).start()

    def _apply(self, generation: int, outcome: PreviewOutcome) -> bool:
        """Render a preview result, unless its inputs have moved on.

        A stale result is dropped but still hands over: the rerun it
        releases is the one that will read the newest inputs.
        """
        self._busy = False
        fresh = generation == self._generation
        if fresh and self._alive():
            self._render(preview_text(outcome))
        if self._again:
            self._again = False
            self._start()
        return bool(GLib.SOURCE_REMOVE)

    # ---------------------------------------------------------- the folder

    def _watch_folder(self, folder: Path) -> None:
        """Monitor the selected folder, replacing any previous monitor."""
        if folder == self._watched and self._monitor is not None:
            return
        self._stop_monitor()
        self._watched = folder
        try:
            monitor = self._create_directory_monitor(folder)
        except GLib.Error as exc:
            # No monitor (an unmonitorable filesystem, a missing folder):
            # the preview stays on its last advisory result until some
            # other refresh event arrives. Polling instead would be worse.
            LOGGER.debug("cannot monitor %s: %s", folder, exc)
            return
        monitor.connect("changed", self._on_folder_changed)
        self._monitor = monitor

    @staticmethod
    def _create_directory_monitor(folder: Path) -> Gio.FileMonitor:
        """Start watching ``folder``; the one place that touches Gio here.

        Raises:
            GLib.Error: If the location cannot be monitored.
        """
        return Gio.File.new_for_path(str(folder)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )

    def _on_folder_changed(self, monitor: Gio.FileMonitor, *_args: object) -> None:
        """A directory event: refresh unless it came from an old folder."""
        if monitor is not self._monitor:
            return  # the previous folder's monitor, still finishing up
        self.request()

    def _stop_monitor(self) -> None:
        """Cancel the directory monitor, if one is running."""
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        self._watched = None
