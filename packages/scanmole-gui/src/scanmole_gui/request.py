"""Immutable scan requests and their CLI argument mapping (no GTK imports).

``MainWindow`` snapshots its widgets into a :class:`ScanRequest` when the
user starts a scan; everything downstream (argv construction, blank
counting) works from that immutable snapshot, never from live widgets, so
mid-scan form changes cannot skew a running session.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def request_argv(request: ScanRequest, scanmole: str) -> list[str]:
    """The exact ``scanmole --json`` command line for a request."""
    argv = [scanmole, "--json"]
    if request.device:
        argv += ["-d", request.device]
    argv += ["--source", request.source]
    argv += mode_argv(request.mode)
    argv += ["-r", str(request.resolution), "--page-size", request.page_size]
    if request.ocr:
        argv.append("--ocr")
        argv += ["-l", request.lang]
    else:
        argv.append("--no-ocr")
    argv.append("--deskew" if request.deskew else "--no-deskew")
    if not request.drop_blanks:
        argv.append("--keep-blanks")
    argv += ["-o", request.output]
    return argv
