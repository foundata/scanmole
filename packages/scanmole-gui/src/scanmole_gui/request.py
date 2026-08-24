"""Immutable scan requests and their CLI argument mapping (no GTK imports).

``MainWindow`` snapshots its widgets into a :class:`ScanRequest` when the
user starts a scan; everything downstream (argv construction, blank
counting) works from that immutable snapshot, never from live widgets, so
mid-scan form changes cannot skew a running session.
"""

from __future__ import annotations

from dataclasses import dataclass

from scanmole.config import AutoSizePreference, DeskewMethod, SheetFlow
from scanmole_gui.modes import mode_argv


@dataclass(frozen=True)
class ScanRequest:
    """One scan as requested by the form, frozen at scan start.

    ``output`` is the full output argument (folder plus filename template);
    the CLI expands the placeholders and picks the next free counter value.
    ``drop_blanks`` mirrors the blank-removal switch: it selects
    ``--keep-blanks`` and decides whether the session counts blank pages.
    """

    device: str | None
    source: str
    mode: str
    resolution: int
    page_size: str
    ocr: bool
    lang: str
    deskew: bool
    drop_blanks: bool
    output: str
    deskew_method: DeskewMethod = "auto"
    """Which mechanism straightens the pages. Carried even while
    ``deskew`` is off, so the setting survives turning it back on, and
    not emitted then because the CLI would ignore it anyway."""
    auto_size_preference: AutoSizePreference = "iso"
    """Family that wins ambiguous automatic sizes; irrelevant (and not
    emitted) for a fixed page size."""
    sheet_flow: SheetFlow = "stack"
    """How many physical sheets the run acquires. The default keeps the
    historic command line and is not emitted."""
    pdfa: bool = True
    """Archival PDF/A output. Produced by the OCR stage, so it only
    takes effect while ``ocr`` is on."""


def request_argv(request: ScanRequest, scanmole: str) -> list[str]:
    """The exact ``scanmole --json`` command line for a request."""
    argv = [scanmole, "--json"]
    if request.device:
        argv += ["-d", request.device]
    argv += ["--source", request.source]
    argv += mode_argv(request.mode)
    if request.sheet_flow != "stack":
        argv += ["--sheet-flow", request.sheet_flow]
    argv += ["-r", str(request.resolution), "--page-size", request.page_size]
    if request.page_size == "auto":
        argv += ["--auto-size-preference", request.auto_size_preference]
    if request.ocr:
        argv.append("--ocr")
        argv += ["-l", request.lang]
    else:
        argv.append("--no-ocr")
    argv.append("--deskew" if request.deskew else "--no-deskew")
    if request.deskew:
        argv += ["--deskew-method", request.deskew_method]
    argv.append("--pdfa" if request.pdfa else "--no-pdfa")
    if not request.drop_blanks:
        argv.append("--keep-blanks")
    argv += ["-o", request.output]
    return argv
