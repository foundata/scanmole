"""Acquire pages from a SANE scanner by driving ``scanimage --batch``."""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError
from scanmole.events import EventWriter
from scanmole.negotiation import (
    Plan,
    Prober,
    Support,
    assess_resolution,
    log_notices,
    negotiate,
    require_supported,
    resolve_faint_plan,
)
from scanmole.options import (
    Capability,
    parse_page_size,
    probe_capabilities,
)
from scanmole.scancommand import EffectiveSettings, build_scan_command
from scanmole.scanstream import run_scanimage
from scanmole.sensors import probe_sensors
from scanmole.sheetflow import (
    COLLECT_IDLE_TIMEOUT_SECONDS,
    CollectCommands,
    CollectController,
    PageOrigin,
    next_page_number,
    page_file_number,
    start_stdin_commands,
)

LOGGER = logging.getLogger(__name__)

__all__ = [
    "EffectiveSettings",
    "ScanResult",
    "build_scan_command",
    "run_scanimage",
    "scan_to_files",
]
"""Acquisition's public surface, including the two command-building
names :mod:`scanmole.scancommand` owns and the streaming entry point
:mod:`scanmole.scanstream` owns: a caller that drives a scan reaches
the command it will drive and the function that drives it through the
same module."""

_PAGE_NAME = re.compile(r"page_\d+\.pnm")
"""The work-directory sweep's own completed-page matcher. The stream
module recognizes announcements with the same pattern, but the two uses
have different owners: this one reconciles the filesystem after a batch,
that one filters a child's stdout."""

# SANE_STATUS_NO_DOCS: the feeder ran empty. After at least one page this is the
# normal end of an ADF batch, not an error.
_NO_DOCS_EXIT = 7


@dataclass(frozen=True)
class ScanResult:
    """The outcome of a completed batch scan.

    Attributes:
        pages: The produced page files, in delivery order.
        settings: The settings the scan actually ran with.
    """

    pages: list[Path]
    settings: EffectiveSettings


def _acquisition_settings(plan: Plan) -> tuple[tuple[str, str], ...]:
    """The plan's acquisition state up to the resolution, as probe settings.

    Exactly the options the scan command applies before the resolution:
    source, final mode, native-enhancement extras and an explicit depth.
    The resolution itself is appended by the caller once it has been
    assessed against this state.
    """
    settings: list[tuple[str, str]] = []
    if plan.source.backend_value is not None:
        settings.append(("--source", plan.source.backend_value))
    if plan.mode.backend_value is not None:
        settings.append(("--mode", plan.mode.backend_value))
    settings.extend(plan.extra_options)
    if plan.depth.backend_value is not None:
        settings.append(("--depth", plan.depth.backend_value))
    return tuple(settings)


def _staged_prober(device: str) -> Prober:
    """A prober for the faint mode's candidate probes: failure means None.

    The bare and source-applied probes stay hard errors (without them no
    scan makes sense); a candidate-mode probe only decides between the
    native and the software faint path, so failure falls back safely.
    """

    def probe(settings: tuple[tuple[str, str], ...]) -> dict[str, Capability] | None:
        try:
            return probe_capabilities(device, settings)
        except (DeviceError, subprocess.SubprocessError, OSError) as exc:
            LOGGER.debug("candidate capability probe failed: %s", exc)
            return None

    return probe


def scan_to_files(
    config: ScanConfig,
    device: str,
    work_dir: Path,
    events: EventWriter,
    on_page: Callable[[Path, PageOrigin], None],
    on_settings: Callable[[EffectiveSettings], None] | None = None,
) -> ScanResult:
    """Scan into ``work_dir`` and return the pages plus the effective settings.

    Emits a ``settings`` event with the values negotiated with the backend
    before the scan starts; the same values are part of the returned
    :class:`ScanResult` so later stages (PDF assembly) can use the dpi the
    pages were actually scanned at. ``on_settings``, when given, receives the
    same values before the first page, so per-page processing can already use
    them. Each page is delivered through ``on_page`` as soon as scanimage
    finishes writing it, together with its :class:`PageOrigin` (which
    acquisition segment produced it, and where within that segment); page
    files that scanimage wrote but did not announce (defensive) are
    delivered after the batch, in name order.

    Raises:
        DeviceError: If ``scanimage`` fails for a reason other than an empty
            feeder at the end of a batch.
        NoPagesError: If no pages were produced.
        ScanMoleError: If ``on_page`` failed for a page. Pages scanned up to
            that point stay in ``work_dir``; the pipeline's recovery contract
            (keep acquired pages, name the path) applies.
    """
    caps = probe_capabilities(device)

    def negotiated(snapshot: dict[str, Capability]) -> Plan:
        return negotiate(
            snapshot,
            source=config.source,
            mode=config.mode,
            resolution=config.resolution,
            lineart_threshold=config.lineart_threshold,
        )

    plan = negotiated(caps)
    faint = plan.mode.requested == "lineart-auto"
    if not faint or plan.source.support is Support.UNSUPPORTED:
        # The faint mode verdict stays provisional until the staged
        # candidate probes below have run; everything else fails fast here.
        require_supported(plan)
    if plan.source.backend_value is not None:
        # Option constraints can depend on the selected source (eSCL devices
        # advertise a different scan window per source: the Brother ADS-4550W
        # reports a 3098.8 mm height for simplex ADF but 355.6 mm for ADF
        # Duplex), so re-read the listing with the negotiated source applied
        # and negotiate again on the authoritative snapshot.
        caps = probe_capabilities(
            device, settings=(("--source", plan.source.backend_value),)
        )
        plan = negotiated(caps)
    if faint:
        # Native faint-text enhancement is recognized on a snapshot taken
        # with the candidate 1-bit mode applied (option activity is
        # state-dependent), so probing goes one stage further; a failed
        # probe falls back to the software path instead of aborting.
        base = (
            (("--source", plan.source.backend_value),)
            if plan.source.backend_value is not None
            else ()
        )
        plan = resolve_faint_plan(plan, caps, _staged_prober(device), base)
    require_supported(plan)
    final_settings = _acquisition_settings(plan)
    if final_settings:
        # Constraints can also depend on the mode (and the other applied
        # options): a backend may offer 50..600 dpi in Color but only a
        # reduced range in Lineart. Reprobe with the complete acquisition
        # state and reassess resolution and geometry from that snapshot.
        # The already-negotiated source, mode, extras and depth stay
        # locked; only the dependent values are read again.
        caps = probe_capabilities(device, settings=final_settings)
        plan = dataclasses.replace(
            plan, resolution=assess_resolution(caps, config.resolution)
        )
    if plan.resolution.support is Support.UNKNOWN:
        # Refuse before feeding paper: without an established physical
        # resolution every PDF page dimension would be a guess. This is a
        # scan-time evidence gate, not an UNSUPPORTED verdict; inactive
        # evidence still never proves a capability is absent.
        raise DeviceError(
            "cannot establish the scanner's physical resolution (no usable "
            "--resolution evidence); refusing to scan because the page "
            "geometry would be untrustworthy"
        )
    window_unverified = False
    if plan.resolution.backend_value is not None:
        # The scan window can depend on the resolution just as it depends
        # on the source and the mode (SANE lets any option change reload
        # every other constraint), so the geometry below is read from a
        # snapshot with the negotiated dpi applied. The resolution is not
        # assessed again from it; the value in this probe is the one the
        # scan will carry.
        final_settings += (("--resolution", plan.resolution.backend_value),)
        with_resolution = _staged_prober(device)(final_settings)
        if with_resolution is not None:
            caps = with_resolution
        else:
            window_unverified = True
    log_notices(plan, LOGGER)
    pattern = str(work_dir / "page_%04d.pnm")
    command, effective = build_scan_command(config, device, caps, pattern, plan)
    if (
        window_unverified
        and parse_page_size(config.page_size) is None
        and effective.window_mm is not None
    ):
        # Automatic page size decides whether a frame was cropped by the
        # hardware by comparing it against this window, so a stale one is
        # not a harmless fallback: a frame that came back at the real,
        # smaller window reads as paper-sized and skips content sizing.
        # A fixed page size never makes that comparison and keeps the
        # tolerant path, and a device without usable x/y evidence carries
        # no window either way, so neither is refused here.
        raise DeviceError(
            "cannot establish the scan window with the effective resolution "
            f"applied ({effective.resolution} dpi): the capability listing "
            "failed and the earlier one may no longer hold. Automatic page "
            "size would size pages against an unverified window, so refusing "
            "to scan -- select a fixed page size (for example --page-size a4) "
            "to continue"
        )
    if on_settings is not None:
        on_settings(effective)
    events.emit(
        "settings",
        device=device,
        source=effective.source,
        mode=effective.mode,
        resolution=effective.resolution,
    )
    LOGGER.info(
        "Scanning from %s (%s, %s, %s dpi) ...",
        device,
        effective.source or config.source,
        effective.mode or config.mode,
        effective.resolution,
    )
    delivered: list[Path] = []
    seen: set[Path] = set()

    def deliver(path: Path, segment: int, segment_start: int) -> None:
        # The frame index within the segment comes from the file number,
        # not the delivery count, so an unannounced frame swept after the
        # batch still lands on the physical sheet it belongs to.
        number = page_file_number(path)
        frame = (
            number - segment_start + 1
            if number is not None  # unreachable None: names are pre-filtered
            else len(delivered) + 1
        )
        delivered.append(path)
        seen.add(path)
        on_page(path, PageOrigin(segment=segment, frame=frame))

    def sweep(segment: int, segment_start: int) -> None:
        # scanimage writes the frame it is scanning to page_NNNN.pnm.part
        # and renames it to page_NNNN.pnm only once the page completed
        # (close, rename, then announce; unchanged since sane-backends
        # 1.0.27), so a file under this name is a finished page whichever
        # way it got here. Missing the announcement is the anomaly, not
        # the page: deliver it and say the announcement was lost, never
        # that the page might be.
        for path in sorted(work_dir.iterdir()):
            if _PAGE_NAME.fullmatch(path.name) and path not in seen:
                LOGGER.warning(
                    "scanimage did not announce %s; recovered the completed "
                    "page after the batch",
                    path.name,
                )
                deliver(path, segment, segment_start)

    def stderr_tail(exit_code: int, stderr_text: str) -> str:
        return (
            "\n".join(stderr_text.strip().splitlines()[-4:])
            or f"scanimage exited {exit_code}"
        )

    if config.sheet_flow == "collect":
        segment = 0

        def acquire() -> int:
            # One collect segment: drain whatever the feeder currently
            # holds, with the numbering continuing from the greatest
            # artifact on disk (unannounced frames included), so nothing
            # is ever overwritten. The negotiated plan is reused as-is;
            # only --batch-start differs between segments.
            nonlocal segment
            segment += 1
            start = next_page_number(work_dir)
            segment_command = (
                command
                if start == 1
                else build_scan_command(config, device, caps, pattern, plan, start)[0]
            )
            before = len(delivered)
            exit_code, stderr_text = run_scanimage(
                segment_command, lambda path: deliver(path, segment, start)
            )
            sweep(segment, start)
            if exit_code == _NO_DOCS_EXIT:
                LOGGER.debug("feeder empty (scanimage exit 7) -- end of segment")
            elif exit_code != 0:
                raise DeviceError(f"scan failed: {stderr_tail(exit_code, stderr_text)}")
            return len(delivered) - before

        conclusive = plan.source.conclusive
        controller = CollectController(
            commands=_collect_commands(),
            # The final acquisition settings make the sensor snapshot
            # source-dependent state, exactly like the last negotiation
            # probe; sensor options themselves are never emitted.
            read_sensors=lambda: probe_sensors(device, final_settings),
            feeder=conclusive
            and plan.source.effective in ("adf", "adf-duplex", "adf-back"),
            # The pairing the pipeline will apply, never a second opinion:
            # a sheet count that contradicts the sizing decision would
            # report one duplex sheet as two.
            duplex=effective.duplex,
            events=events,
            idle_seconds=COLLECT_IDLE_TIMEOUT_SECONDS,
        )
        controller.run(acquire)
    else:
        exit_code, stderr_text = run_scanimage(
            command, lambda path: deliver(path, 1, 1)
        )
        sweep(1, 1)
        if exit_code == _NO_DOCS_EXIT and delivered:
            LOGGER.debug("feeder empty (scanimage exit 7) -- normal end of batch")
        elif exit_code not in (0, _NO_DOCS_EXIT):
            raise DeviceError(f"scan failed: {stderr_tail(exit_code, stderr_text)}")
    if not delivered:
        raise NoPagesError("no pages were scanned -- is there paper in the feeder?")
    return ScanResult(pages=delivered, settings=effective)


def _collect_commands() -> CollectCommands:
    """The standard-input control channel of a collect run (test seam)."""
    return start_stdin_commands(sys.stdin)
