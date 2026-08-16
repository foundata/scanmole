"""Acquire pages from a SANE scanner by driving ``scanimage --batch``."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.external import SCAN_TIMEOUT_SECONDS
from scanmole.options import (
    Capability,
    format_mm,
    map_mode,
    map_source,
    parse_page_size,
    probe_capabilities,
    snap_resolution,
)

LOGGER = logging.getLogger(__name__)

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
) -> tuple[list[str], EffectiveSettings]:
    """Assemble the ``scanimage`` command for a batch scan.

    Only options the device actually advertises (per ``caps``) are included.

    Returns:
        The command and the settings the scan will actually run with.
    """
    command = ["scanimage", "-d", device]

    source = map_source(config.source, caps)
    if source is not None:
        command += ["--source", source]
    mode = map_mode(config.mode, caps)
    if mode is not None:
        command += ["--mode", mode]
    resolution = snap_resolution(config.resolution, caps)
    if resolution is not None:
        command += ["--resolution", str(resolution)]

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
    width_cap = _window_cap(caps.get("page-width"), caps.get("x"))
    height_cap = _window_cap(caps.get("page-height"), caps.get("y"))
    for option, value, capability in (
        ("--page-width", width, caps.get("page-width")),
        ("--page-height", height, caps.get("page-height")),
        ("-x", width, width_cap if "x" in caps else None),
        ("-y", height, height_cap if "y" in caps else None),
    ):
        if capability is None:
            continue
        if value == float("inf") and (
            capability.kind != "range" or capability.maximum is None
        ):
            continue  # no known maximum: let the backend's default window apply
        command += [option, format_mm(value, capability, option)]
    if size is None and "ald" in caps:
        # Auto page size: let the scanner detect the paper's lower edge, so
        # frames come back at true paper length instead of the padded window.
        # Essential for native lineart, where the padding below the paper is
        # bit-identical to the page's own white margin and software cropping
        # cannot tell them apart (verified on the iX100: 297 mm instead of
        # an 895 mm frame).
        command.append("--ald=yes")

    if config.despeckle > 0 and "swdespeck" in caps:
        command.append(f"--swdespeck={config.despeckle}")
    if "swdeskew" in caps:
        command.append(f"--swdeskew={'yes' if config.deskew else 'no'}")
    if "swcrop" in caps:
        command.append(f"--swcrop={'yes' if config.crop else 'no'}")

    command += ["--format=pnm", f"--batch={batch_pattern}", "--batch-print"]
    if config.source == "flatbed":
        command.append("--batch-count=1")  # a flatbed never reports "feeder empty"
    return command, EffectiveSettings(source=source, mode=mode, resolution=resolution)


def run_scanimage(
    command: list[str], on_page: Callable[[Path], None]
) -> tuple[int, str]:
    """Run a batch scan, reporting each completed page while it runs.

    ``--batch-print`` makes scanimage print each page's file name to stdout as
    soon as the page is written; ``on_page`` is called with that path from a
    reader thread, so callers can analyze pages and stream progress while the
    rest of the batch is still scanning. stderr is logged as progress.

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
                page_failure = exc
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
    with process:  # closes the pipes after the readers are done
        stdout, stderr = process.stdout, process.stderr
        if stdout is None or stderr is None:  # unreachable: PIPE yields streams
            raise DeviceError("scanimage produced no output streams")
        readers = [
            threading.Thread(target=pump_stderr, args=(stderr,), daemon=True),
            threading.Thread(target=pump_stdout, args=(stdout,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            exit_code = process.wait(timeout=SCAN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise DeviceError(f"scan timed out after {SCAN_TIMEOUT_SECONDS}s") from exc
        except BaseException:  # SIGINT/SIGTERM: never leave scanimage running
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        for reader in readers:
            reader.join(timeout=10)
    if page_failure is not None:
        if isinstance(page_failure, ScanMoleError):
            raise page_failure
        raise ScanMoleError(f"page processing failed: {page_failure}") from page_failure
    return exit_code, "\n".join(lines)


def scan_to_files(
    config: ScanConfig,
    device: str,
    work_dir: Path,
    events: EventWriter,
    on_page: Callable[[Path], None],
) -> ScanResult:
    """Scan into ``work_dir`` and return the pages plus the effective settings.

    Emits a ``settings`` event with the values negotiated with the backend
    before the scan starts; the same values are part of the returned
    :class:`ScanResult` so later stages (PDF assembly) can use the dpi the
    pages were actually scanned at. Each page is delivered through ``on_page``
    as soon as scanimage finishes writing it; page files that scanimage wrote
    but did not announce (defensive) are delivered after the batch, in name
    order.

    Raises:
        DeviceError: If ``scanimage`` fails for a reason other than an empty
            feeder at the end of a batch.
        NoPagesError: If no pages were produced.
        ScanMoleError: If ``on_page`` failed for a page. Pages scanned up to
            that point stay in ``work_dir``; the pipeline's recovery contract
            (keep acquired pages, name the path) applies.
    """
    caps = probe_capabilities(device)
    source = map_source(config.source, caps)
    if source is not None:
        # Option constraints can depend on the selected source (eSCL devices
        # advertise a different scan window per source: the ADS-4550W reports
        # a 3098.8 mm height for simplex ADF but 355.6 mm for ADF Duplex), so
        # re-read the listing with the mapped source applied.
        caps = probe_capabilities(device, source=source)
    pattern = str(work_dir / "page_%04d.pnm")
    command, effective = build_scan_command(config, device, caps, pattern)
    events.emit(
        "settings",
        device=device,
        source=effective.source,
        mode=effective.mode,
        resolution=effective.resolution,
    )
    LOGGER.info(
        "Scanning from %s (%s, %s, %d dpi) ...",
        device,
        effective.source or config.source,
        effective.mode or config.mode,
        effective.resolution if effective.resolution is not None else config.resolution,
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
