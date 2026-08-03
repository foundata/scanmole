"""Acquire pages from a SANE scanner by driving ``scanimage --batch``."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError
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


def build_scan_command(
    config: ScanConfig,
    device: str,
    caps: dict[str, Capability],
    batch_pattern: str,
) -> tuple[list[str], dict[str, str | int | None]]:
    """Assemble the ``scanimage`` command for a batch scan.

    Only options the device actually advertises (per ``caps``) are included.

    Returns:
        The command and the effective settings: the backend's source and mode
        strings and the dpi actually requested. A setting is ``None`` when the
        device does not expose the option at all.
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

    width, height = parse_page_size(config.page_size)
    if "page-width" in caps:
        command += [
            "--page-width",
            format_mm(width, caps["page-width"], "--page-width"),
        ]
    if "page-height" in caps:
        command += [
            "--page-height",
            format_mm(height, caps["page-height"], "--page-height"),
        ]
    if "x" in caps:
        command += ["-x", format_mm(width, caps["x"], "-x")]
    if "y" in caps:
        command += ["-y", format_mm(height, caps["y"], "-y")]

    if config.despeckle > 0 and "swdespeck" in caps:
        command.append(f"--swdespeck={config.despeckle}")
    if "swdeskew" in caps:
        command.append(f"--swdeskew={'yes' if config.deskew else 'no'}")
    if "swcrop" in caps:
        command.append(f"--swcrop={'yes' if config.crop else 'no'}")

    command += ["--format=pnm", f"--batch={batch_pattern}", "--batch-print"]
    if config.source == "flatbed":
        command.append("--batch-count=1")  # a flatbed never reports "feeder empty"
    effective: dict[str, str | int | None] = {
        "source": source,
        "mode": mode,
        "resolution": resolution,
    }
    return command, effective


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
    """
    LOGGER.debug("+ %s", shlex.join(command))
    lines: list[str] = []

    def pump_stderr(pipe: IO[str]) -> None:
        for raw in pipe:
            line = raw.rstrip("\n")
            lines.append(line)
            if _SCANNED_PAGE.match(line):
                LOGGER.info("%s ...", line.split(".")[0])
            else:
                LOGGER.debug("scanimage: %s", line)

    def pump_stdout(pipe: IO[str]) -> None:
        for raw in pipe:
            name = raw.strip()
            if not name or not _PAGE_NAME.fullmatch(Path(name).name):
                continue
            try:
                on_page(Path(name))
            except Exception:
                LOGGER.exception("page callback failed for %s", name)

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
            raise DeviceError(
                f"scan timed out after {SCAN_TIMEOUT_SECONDS}s"
            ) from exc
        for reader in readers:
            reader.join(timeout=10)
    return exit_code, "\n".join(lines)


def scan_to_files(
    config: ScanConfig,
    device: str,
    work_dir: Path,
    events: EventWriter,
    on_page: Callable[[Path], None],
) -> list[Path]:
    """Scan into ``work_dir`` and return the produced page files, in order.

    Emits a ``settings`` event with the values negotiated with the backend
    before the scan starts. Each page is delivered through ``on_page`` as soon
    as scanimage finishes writing it; page files that scanimage wrote but did
    not announce (defensive) are delivered after the batch, in name order.

    Raises:
        DeviceError: If ``scanimage`` fails for a reason other than an empty
            feeder at the end of a batch.
        NoPagesError: If no pages were produced.
    """
    caps = probe_capabilities(device)
    pattern = str(work_dir / "page_%04d.pnm")
    command, effective = build_scan_command(config, device, caps, pattern)
    events.emit("settings", device=device, **effective)
    LOGGER.info(
        "Scanning from %s (%s, %s, %d dpi) ...",
        device,
        config.source,
        config.mode,
        config.resolution,
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
    return delivered
