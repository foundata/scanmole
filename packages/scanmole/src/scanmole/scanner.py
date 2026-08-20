"""Acquire pages from a SANE scanner by driving ``scanimage --batch``."""

from __future__ import annotations

import dataclasses
import logging
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError, ScanMoleError, Terminated
from scanmole.events import EventWriter
from scanmole.external import SCAN_TIMEOUT_SECONDS
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
    active_capability,
    format_mm,
    is_flatbed_source,
    parse_page_size,
    probe_capabilities,
)

LOGGER = logging.getLogger(__name__)

REAP_GRACE_SECONDS = 5.0
"""The scanimage child's own cleanup window between TERM and KILL."""

_FAILURE_POLL_SECONDS = 0.2
"""Wait slice of the scan wait; bounds how late a callback failure or the
absolute scan deadline is noticed while the child keeps running."""

_PAGE_NAME = re.compile(r"page_\d+\.pnm")
_SCANNED_PAGE = re.compile(r"^Scanned page \d+")

# SANE_STATUS_NO_DOCS: the feeder ran empty. After at least one page this is the
# normal end of an ADF batch, not an error.
_NO_DOCS_EXIT = 7


@dataclass(frozen=True)
class EffectiveSettings:
    """The values actually negotiated with the backend for a scan.

    A field is ``None`` when the device does not expose the option at all.
    ``resolution`` is the dpi actually requested after capability snapping,
    which may differ from the dpi the user asked for.
    """

    source: str | None
    mode: str | None
    resolution: int | None
    window_mm: tuple[float, float] | None = None
    """The clamped ``-x``/``-y`` scan window actually requested, if known.

    Lets the pipeline recognize frames that came back at the full window:
    the proof that no hardware paper-length detection took place.
    """
    deskew_applied: bool = False
    """Whether a backend deskew option took the deskew request.

    Decides the next step of the deskew cascade: without a backend option
    the pipeline hands the job to OCR, and failing that warns, so the
    request is never a silent no-op.
    """
    faint_native: bool = False
    """Whether a native text enhancement serves the faint request.

    On this path 1-bit frames are the enhanced result the user asked for;
    on every other ``lineart-auto`` path an arriving 1-bit frame proves
    the faint request cannot be satisfied and the pipeline must stop.
    """


@dataclass(frozen=True)
class ScanResult:
    """The outcome of a completed batch scan.

    Attributes:
        pages: The produced page files, in delivery order.
        settings: The settings the scan actually ran with.
    """

    pages: list[Path]
    settings: EffectiveSettings


def _window_cap(page: Capability | None, axis: Capability | None) -> Capability | None:
    """Pick the capability that carries the device's true window limit.

    Prefers the page geometry capability when it is a range with a known
    maximum, falling back to the axis (``-x``/``-y``) capability otherwise.
    """
    if page is not None and page.kind == "range" and page.maximum is not None:
        return page
    return axis


def build_scan_command(
    config: ScanConfig,
    device: str,
    caps: dict[str, Capability],
    batch_pattern: str,
    plan: Plan | None = None,
) -> tuple[list[str], EffectiveSettings]:
    """Assemble the ``scanimage`` command for a batch scan.

    Only options the device actively advertises (per ``caps``) are included.
    The source, mode and resolution come from the negotiated ``plan`` (one
    is computed from ``caps`` when the caller has none), so fallback policy
    lives in one place.

    Raises:
        DeviceError: If the plan marks the source or mode UNSUPPORTED.

    Returns:
        The command and the settings the scan will actually run with.
    """
    if plan is None:
        plan = negotiate(
            caps,
            source=config.source,
            mode=config.mode,
            resolution=config.resolution,
            lineart_threshold=config.lineart_threshold,
        )
    require_supported(plan)
    command = ["scanimage", "-d", device]

    source = plan.source.backend_value
    if source is not None:
        command += ["--source", source]
    mode = plan.mode.backend_value
    if mode is not None:
        command += ["--mode", mode]
    # A native faint-text enhancement's ordered settings follow the mode
    # they were verified against; the adaptive faint path pins the 8-bit
    # depth the guarded threshold needs.
    for extra_option, extra_value in plan.extra_options:
        command += [extra_option, extra_value]
    if plan.depth.backend_value is not None:
        command += ["--depth", plan.depth.backend_value]
    if plan.resolution.backend_value is not None:
        command += ["--resolution", plan.resolution.backend_value]
    # The settings carry the *established* dpi (empty for UNKNOWN): a
    # fixed backend contributes it without any --resolution being emitted,
    # and the requested dpi never masquerades as an established one.
    resolution = (
        int(plan.resolution.effective) if plan.resolution.effective.isdigit() else None
    )

    size = parse_page_size(config.page_size)
    if size is None:
        # Auto page size: scan the device's full window (it clamps oversized
        # requests itself); the pipeline crops each page to the paper edges.
        width, height = float("inf"), float("inf")
    else:
        width, height = size
    # Some backends cap the advertised -x/-y ranges at the current window
    # (fujitsu reports A4 height until --page-height is raised), so the scan
    # area is clamped against the page geometry maxima where the backend has
    # them; --page-width/--page-height are emitted first to extend the window.
    width_cap = _window_cap(
        active_capability(caps, "page-width"), active_capability(caps, "x")
    )
    height_cap = _window_cap(
        active_capability(caps, "page-height"), active_capability(caps, "y")
    )
    has_x = active_capability(caps, "x") is not None
    has_y = active_capability(caps, "y") is not None
    window: dict[str, float] = {}
    for option, value, capability in (
        ("--page-width", width, active_capability(caps, "page-width")),
        ("--page-height", height, active_capability(caps, "page-height")),
        ("-x", width, width_cap if has_x else None),
        ("-y", height, height_cap if has_y else None),
    ):
        if capability is None:
            continue
        if value == float("inf") and (
            capability.kind != "range" or capability.maximum is None
        ):
            continue  # no known maximum: let the backend's default window apply
        rendered = format_mm(value, capability, option)
        command += [option, rendered]
        if option in ("-x", "-y"):
            window[option] = float(rendered)
    if size is None and active_capability(caps, "ald") is not None:
        # Auto page size: let the scanner detect the paper's lower edge, so
        # frames come back at true paper length instead of the padded window.
        # Essential for native lineart, where the padding below the paper is
        # bit-identical to the page's own white margin and software cropping
        # cannot tell them apart (verified on the ScanSnap iX100: 297 mm
        # instead of an 895 mm frame).
        command.append("--ald=yes")
    if size is None and active_capability(caps, "adf-crp") is not None:
        # Same idea on the epsonds backend ("ADF auto cropping"): the device
        # crops to the detected paper bounds itself. White-backing scanners
        # (Epson DS series) need this, because software edge detection cannot
        # tell white backing from white paper.
        command.append("--adf-crp=yes")

    if config.despeckle > 0 and active_capability(caps, "swdespeck") is not None:
        command.append(f"--swdespeck={config.despeckle}")
    deskew_applied = False
    if active_capability(caps, "swdeskew") is not None:
        command.append(f"--swdeskew={'yes' if config.deskew else 'no'}")
        deskew_applied = config.deskew
    if active_capability(caps, "adf-skew") is not None:
        # epsonds' hardware skew correction, same contract as --swdeskew.
        command.append(f"--adf-skew={'yes' if config.deskew else 'no'}")
        deskew_applied = deskew_applied or config.deskew
    if active_capability(caps, "swcrop") is not None:
        command.append(f"--swcrop={'yes' if config.crop else 'no'}")

    command += ["--format=pnm", f"--batch={batch_pattern}", "--batch-print"]
    # Keyed on the *mapped* source: a feeder request degraded to the flatbed
    # (flatbed-only device) must not batch-scan "infinity pages" on hardware
    # that never reports "feeder empty".
    flatbed = plan.source.effective == "flatbed" or (
        source is not None and is_flatbed_source(source)
    )
    if flatbed:
        command.append("--batch-count=1")  # a flatbed never reports "feeder empty"
    return command, EffectiveSettings(
        source=source,
        mode=mode,
        resolution=resolution,
        window_mm=(window["-x"], window["-y"]) if len(window) == 2 else None,
        deskew_applied=deskew_applied,
        faint_native=(
            plan.mode.requested == "lineart-auto"
            and plan.mode.support is Support.NATIVE
        ),
    )


def run_scanimage(
    command: list[str], on_page: Callable[[Path], None]
) -> tuple[int, str]:
    """Run a batch scan, reporting each completed page while it runs.

    ``--batch-print`` makes scanimage print each page's file name to stdout as
    soon as the page is written; ``on_page`` is called with that path from a
    reader thread, so callers can analyze pages and stream progress while the
    rest of the batch is still scanning. stderr is logged as progress.

    Lifecycle invariant: no reader thread and no delivered page callback is
    still active when this function returns or raises. Every ending (normal
    exit, timeout, callback failure, SIGINT/SIGTERM or any other exception)
    goes through one shutdown-and-drain path: the child is reaped, page
    announcements already in the pipe are delivered in order, both readers
    finish, then the pipes close. An interrupt during that drain is recorded
    and raised afterwards; it never skips the drain and never replaces an
    earlier terminating cause.

    Returns:
        The exit code and the collected stderr text.

    Raises:
        DeviceError: If the scan exceeds :data:`SCAN_TIMEOUT_SECONDS`.
        ScanMoleError: If ``on_page`` raised. A domain error propagates as-is;
            anything else is chained into a :class:`ScanMoleError`. The scan
            subprocess is terminated first, so no further pages are acquired.
    """
    LOGGER.debug("+ %s", shlex.join(command))
    lines: list[str] = []
    failure_lock = threading.Lock()
    failure_event = threading.Event()
    page_failure: Exception | None = None

    def pump_stderr(pipe: IO[str]) -> None:
        for raw in pipe:
            line = raw.rstrip("\n")
            lines.append(line)
            if _SCANNED_PAGE.match(line):
                LOGGER.info("%s ...", line.split(".")[0])
            else:
                LOGGER.debug("scanimage: %s", line)

    def pump_stdout(pipe: IO[str]) -> None:
        # A failing page callback must fail the whole batch: continuing would
        # let the run end in a misleading success, "all blank" or empty-feeder
        # result. Record the error for the controlling thread, stop the scan
        # and stop delivering pages.
        nonlocal page_failure
        for raw in pipe:
            name = raw.strip()
            if not name or not _PAGE_NAME.fullmatch(Path(name).name):
                continue
            try:
                on_page(Path(name))
            except Exception as exc:
                with failure_lock:
                    page_failure = exc
                failure_event.set()  # wakes the controller's deadline wait
                LOGGER.debug("page callback failed for %s", name, exc_info=True)
                process.terminate()
                break

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    stdout, stderr = process.stdout, process.stderr
    if stdout is None or stderr is None:  # unreachable: PIPE yields streams
        process.kill()
        process.wait()
        raise DeviceError("scanimage produced no output streams")
    # Non-daemon on purpose: their lifetime is owned here and every path
    # below joins them before returning or raising.
    stdout_reader = threading.Thread(
        target=pump_stdout, args=(stdout,), name="scanmole-stdout-reader"
    )
    stderr_reader = threading.Thread(
        target=pump_stderr, args=(stderr,), name="scanmole-stderr-reader"
    )
    cause: BaseException | None = None
    exit_code = -1
    started: list[threading.Thread] = []
    try:
        for reader in (stdout_reader, stderr_reader):
            reader.start()
            started.append(reader)

        def wait_for_scan() -> int:
            """Reap the child under one absolute deadline, watching failures.

            The reader terminates the child when a page callback fails,
            but a child ignoring TERM must not keep the controller inside
            an hour-long wait that then misreports the processing error
            as a scan timeout: the failure event ends the wait promptly
            and the shutdown path below applies the KILL escalation. A
            genuine timeout that fires first keeps precedence (the event
            is only honored while the deadline has not passed).
            """
            deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
            while not failure_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, SCAN_TIMEOUT_SECONDS)
                try:
                    return process.wait(timeout=min(_FAILURE_POLL_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
            return -1  # callback failure: reaped and reported below

        try:
            exit_code = wait_for_scan()
        except subprocess.TimeoutExpired as exc:
            timeout_error = DeviceError(f"scan timed out after {SCAN_TIMEOUT_SECONDS}s")
            timeout_error.__cause__ = exc
            cause = timeout_error
            process.kill()
        except BaseException as exc:  # SIGINT/SIGTERM: stop acquiring, drain
            cause = exc
            process.terminate()
        if cause is None and failure_event.is_set():
            # The callback failure ended the wait: promote it to the
            # terminating cause now, so an interrupt landing in the drain
            # below cannot replace the real diagnosis. A timeout or
            # interrupt that fired first keeps precedence (the branches
            # above already recorded it).
            with failure_lock:
                failure = page_failure
            if failure is not None:
                if isinstance(failure, ScanMoleError):
                    cause = failure
                else:
                    promoted = ScanMoleError(f"page processing failed: {failure}")
                    promoted.__cause__ = failure
                    cause = promoted
    except BaseException as exc:  # an interrupt outside the wait itself
        cause = exc
        process.terminate()
    finally:
        # The one unconditional shutdown-and-drain path. Whatever stopped
        # the batch (normal exit, timeout, callback failure, interrupt):
        # reap the child, let the stdout reader consume every announcement
        # already in the pipe and finish its callbacks in order, drain the
        # stderr reader, and only then close the pipes. Returning or
        # raising earlier would race the caller's recovery logic against a
        # page that is still being analyzed; a bounded join is exactly
        # that bug with extra steps. Interrupts arriving during the drain
        # are recorded (the first becomes the terminating cause when none
        # exists yet) and the drain continues.
        def absorbing(step: Callable[[], object]) -> None:
            """Run one drain step; interrupts retry it, failures never do.

            KeyboardInterrupt (SIGINT) and Terminated (the CLI's SIGTERM
            translation) are recorded and the step retried, so hammering
            Ctrl-C cannot skip the drain. Anything else is a genuine
            cleanup failure: recorded once as the terminating cause when
            none exists yet and not retried (retrying a persistent failure,
            e.g. joining a reader that never started, would loop forever);
            the remaining drain steps still run.
            """
            nonlocal cause
            while True:
                try:
                    step()
                    return
                except (KeyboardInterrupt, Terminated) as exc:
                    if cause is None:
                        cause = exc
                except BaseException as exc:
                    if cause is None:
                        cause = exc
                    return

        def reap() -> None:
            if process.poll() is not None:
                return
            try:
                # The child's own cleanup window after the TERM.
                process.wait(timeout=REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        absorbing(reap)
        for reader in started:  # join only what actually started
            absorbing(reader.join)
        absorbing(lambda: _close_stream(stdout))
        absorbing(lambda: _close_stream(stderr))
    if cause is not None:
        raise cause
    with failure_lock:
        failure = page_failure
    if failure is not None:
        if isinstance(failure, ScanMoleError):
            raise failure
        raise ScanMoleError(f"page processing failed: {failure}") from failure
    return exit_code, "\n".join(lines)


def _close_stream(stream: IO[str]) -> None:
    """Close one child pipe; a tiny seam so tests can inject failures."""
    stream.close()


def _acquisition_settings(plan: Plan) -> tuple[tuple[str, str], ...]:
    """The plan's complete ordered acquisition state, as probe settings.

    Exactly the options the scan command will apply before geometry:
    source, final mode, native-enhancement extras and an explicit depth.
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
    on_page: Callable[[Path], None],
    on_settings: Callable[[EffectiveSettings], None] | None = None,
) -> ScanResult:
    """Scan into ``work_dir`` and return the pages plus the effective settings.

    Emits a ``settings`` event with the values negotiated with the backend
    before the scan starts; the same values are part of the returned
    :class:`ScanResult` so later stages (PDF assembly) can use the dpi the
    pages were actually scanned at. ``on_settings``, when given, receives the
    same values before the first page, so per-page processing can already use
    them. Each page is delivered through ``on_page`` as soon as scanimage
    finishes writing it; page files that scanimage wrote but did not announce
    (defensive) are delivered after the batch, in name order.

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
    log_notices(plan, LOGGER)
    pattern = str(work_dir / "page_%04d.pnm")
    command, effective = build_scan_command(config, device, caps, pattern, plan)
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

    def deliver(path: Path) -> None:
        delivered.append(path)
        seen.add(path)
        on_page(path)

    exit_code, stderr_text = run_scanimage(command, deliver)

    for path in sorted(work_dir.iterdir()):
        if _PAGE_NAME.fullmatch(path.name) and path not in seen:
            deliver(path)

    if exit_code == _NO_DOCS_EXIT and delivered:
        LOGGER.debug("feeder empty (scanimage exit 7) -- normal end of batch")
    elif exit_code not in (0, _NO_DOCS_EXIT):
        tail = (
            "\n".join(stderr_text.strip().splitlines()[-4:])
            or f"scanimage exited {exit_code}"
        )
        raise DeviceError(f"scan failed: {tail}")
    if not delivered:
        raise NoPagesError("no pages were scanned -- is there paper in the feeder?")
    return ScanResult(pages=delivered, settings=effective)
