"""Command-line interface for ScanMole.

Builds a :class:`~scanmole.config.ScanConfig` from parsed arguments, configures
logging (diagnostics to stderr) and runs the pipeline. Machine-readable JSON
events go to stdout via :class:`~scanmole.events.EventWriter`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import override

from scanmole import BYLINE, __version__
from scanmole.config import ScanConfig
from scanmole.devices import (
    DEVICE_ENV_VAR,
    is_real_device,
    list_devices,
    pick_default_device,
)
from scanmole.errors import InputError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.external import require_tools
from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE, expand_template, has_counter
from scanmole.pipeline import run_pipeline

LOGGER = logging.getLogger("scanmole")

_INTERRUPTED_EXIT_CODE = 130  # 128 + SIGINT
_TERMINATED_EXIT_CODE = 143  # 128 + SIGTERM


class _Terminated(Exception):
    """Raised by the SIGTERM handler so cleanup handlers run before exit.

    The GUI (and process supervisors) stop a run with SIGTERM. Python's
    default disposition would kill the interpreter without unwinding, leaving
    the scanimage child running and the work directory behind.
    """


def _install_sigterm_handler() -> None:
    """Convert SIGTERM into :class:`_Terminated`."""

    def raise_terminated(signum: int, frame: FrameType | None) -> None:
        raise _Terminated

    try:
        signal.signal(signal.SIGTERM, raise_terminated)
    except ValueError:  # not the main thread (embedded use); keep the default
        pass


class _LevelPrefixFormatter(logging.Formatter):
    """Plain text for INFO/DEBUG; a ``warning:``/``error:`` prefix above that."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` with a level prefix only for WARNING and above."""
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{record.levelname.lower()}: {message}"
        return message


def configure_logging(*, verbose: bool) -> None:
    """Send diagnostics to stderr at DEBUG (verbose) or INFO level."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_LevelPrefixFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    """Construct the ScanMole argument parser."""
    parser = argparse.ArgumentParser(
        prog="scanmole",
        description="Scan from a SANE scanner (or image files) to a searchable PDF.",
        epilog=(
            "examples:\n"
            "  scanmole                       scan ADF duplex -> "
            "./2026-08-15_scan_001.pdf\n"
            "  scanmole invoice               -> ./invoice.pdf\n"
            "  scanmole '{YYYY}-{MM}_scan_{NN}'       -> ./2026-08_scan_01.pdf\n"
            "  scanmole --source flatbed --mode gray -r 150 --no-ocr -o test.pdf\n"
            "  scanmole --from-images p1.png p2.png -o doc.pdf\n"
            "  scanmole --list-devices --json\n"
            "\n"
            "filename placeholders: {YYYY} {MM} {DD} (date), {hh} {mm} {ss} "
            "(time), {N}/{NN}/... (zero-padded auto-number), {device}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "outbase",
        nargs="?",
        metavar="OUTBASE",
        help="output file name or template (.pdf appended if missing)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help=(
            "output PDF file or template "
            f"(default: {DEFAULT_OUTPUT_TEMPLATE} in the current directory)"
        ),
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list SANE devices and exit",
    )
    parser.add_argument(
        "-d",
        "--device",
        # The ${DEVICE_ENV_VAR} fallback happens at device selection time, not
        # here: an explicit -d must stay distinguishable from the environment.
        help=f"SANE device (default: ${DEVICE_ENV_VAR}, else first real device)",
    )
    parser.add_argument(
        "--source",
        choices=["adf-duplex", "adf", "adf-back", "flatbed"],
        default="adf-duplex",
        help="paper source (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["lineart", "gray", "color"],
        default="lineart",
        help="scan mode (default: %(default)s)",
    )
    parser.add_argument(
        "-r",
        "--resolution",
        type=int,
        default=300,
        metavar="N",
        help="resolution in dpi (default: %(default)s)",
    )
    parser.add_argument(
        "--page-size",
        default="auto",
        metavar="SIZE",
        help=(
            "auto (detect the paper edges), a4|a5|a6|letter|legal, or WxH in "
            "mm (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--despeckle",
        type=int,
        default=1,
        metavar="N",
        help="despeckle radius, 0 = off (default: %(default)s)",
    )
    parser.add_argument(
        "--deskew",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="software deskew (default: off)",
    )
    parser.add_argument(
        "--crop",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="software auto-crop (default: off)",
    )
    parser.add_argument(
        "--ocr",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run OCR (default: on)",
    )
    parser.add_argument(
        "-l",
        "--lang",
        default="deu+eng",
        help="OCR language(s), e.g. deu or deu+eng (default: %(default)s)",
    )
    parser.add_argument(
        "--rotate-pages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="let OCR auto-rotate pages (default: on)",
    )
    parser.add_argument(
        "--optimize",
        type=int,
        choices=range(4),
        default=1,
        metavar="0..3",
        help="ocrmypdf optimization level (default: %(default)s)",
    )
    parser.add_argument(
        "--pdfa",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="produce archival PDF/A output; only applies with OCR (default: on)",
    )
    parser.add_argument(
        "--lineart-threshold",
        type=float,
        default=0.5,
        metavar="F",
        help=(
            "black/white cutoff (fraction of full brightness) for converting "
            "pages in software when the device cannot scan 1-bit lineart "
            "itself; 0 keeps the device's gray/color output (default: "
            "%(default)s)"
        ),
    )
    parser.add_argument(
        "--blank-threshold",
        type=float,
        default=0.995,
        metavar="F",
        help=(
            "mean brightness above which a page is blank; 0 disables "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--keep-blanks",
        action="store_true",
        help="do not drop blank pages",
    )
    parser.add_argument(
        "--from-images",
        nargs="+",
        metavar="FILE",
        help="skip scanning; build the PDF from these images (in given order)",
    )
    parser.add_argument(
        "--keep-images",
        metavar="DIR",
        help="copy kept page images to DIR",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON-lines events on stdout, logs on stderr",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose logging to stderr",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}\n{BYLINE}",
    )
    return parser


def _unique_output(path: Path) -> Path:
    """Reserve and return ``path`` or the next free ``name_2.pdf``, ...

    The chosen name is reserved by creating it empty with ``O_EXCL``, so two
    concurrent runs can never pick the same output file; a plain existence
    check would let both pass before either has written anything. The
    pipeline later replaces the empty file atomically with the finished PDF;
    :func:`main` removes it again when no PDF was published.

    Raises:
        InputError: If the output location is not writable.
    """
    number = 2
    candidate = path
    while True:
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
            number += 1
        except OSError as exc:
            raise InputError(f"cannot create output file {candidate}: {exc}") from exc
        else:
            return candidate


def _discard_unused_reservation(output: Path | None) -> None:
    """Remove a reserved output file that never received a PDF.

    The reservation is the still-empty file :func:`_unique_output` created;
    once the pipeline has published, the file has content and stays.
    """
    if output is None:
        return
    try:
        if output.stat().st_size == 0:
            output.unlink()
    except OSError:
        LOGGER.debug("could not clean up %s", output, exc_info=True)


def _as_pdf_path(name: str) -> Path:
    """Turn an expanded output name into an absolute path ending in .pdf."""
    path = Path(name).expanduser()
    if path.suffix.lower() != ".pdf":
        path = path.with_name(path.name + ".pdf")
    return path.resolve()


def _resolve_output(args: argparse.Namespace) -> Path:
    """Expand the output template and reserve a final, non-overwriting path.

    ``-o``/``OUTBASE`` may contain the documented filename placeholders. A
    template with an ``NN``/``NNN`` counter claims the next free number; any
    other name falls back to the ``_2``, ``_3``, ... suffix.

    Raises:
        InputError: If both ``-o`` and a positional base name are given, the
            template needs a device on a run without one, or the output
            location is not writable.
    """
    if args.output and args.outbase:
        raise InputError("give either -o/--output or a positional OUTBASE, not both")
    template = args.output or args.outbase or DEFAULT_OUTPUT_TEMPLATE
    device: str | None = args.device or None
    if "{device}" in template and device is None:
        if args.from_images is not None:
            raise InputError(
                "the {device} placeholder needs a scanner run; "
                "--from-images has no device"
            )
        device = pick_default_device()
    when = datetime.now().astimezone()

    def expand(counter: int) -> Path:
        try:
            name = expand_template(template, when=when, counter=counter, device=device)
        except ValueError as exc:  # unreachable: {device} was resolved above
            raise InputError(str(exc)) from exc
        return _as_pdf_path(name)

    if not has_counter(template):
        return _unique_output(expand(1))
    number = 1
    while True:
        candidate = expand(number)
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            number += 1
        except OSError as exc:
            raise InputError(f"cannot create output file {candidate}: {exc}") from exc
        else:
            return candidate


def _build_config(args: argparse.Namespace) -> ScanConfig:
    """Translate parsed arguments into a :class:`ScanConfig`.

    Raises:
        InputError: If ``--from-images`` is combined with an explicit device,
            or ``--lineart-threshold`` is out of range.
    """
    if args.from_images is not None and args.device is not None:
        raise InputError(
            "--from-images does not scan; do not combine it with -d/--device"
        )
    if not 0 <= args.lineart_threshold < 1:
        raise InputError(
            "--lineart-threshold must be 0 (off) or a fraction below 1, "
            f"got {args.lineart_threshold}"
        )
    from_images = (
        tuple(Path(image) for image in args.from_images)
        if args.from_images is not None
        else None
    )
    keep_images = Path(args.keep_images) if args.keep_images else None
    return ScanConfig(
        device=args.device,
        source=args.source,
        mode=args.mode,
        resolution=args.resolution,
        page_size=args.page_size,
        despeckle=args.despeckle,
        deskew=args.deskew,
        crop=args.crop,
        ocr=args.ocr,
        lang=args.lang,
        rotate_pages=args.rotate_pages,
        optimize=args.optimize,
        pdfa=args.pdfa,
        blank_threshold=args.blank_threshold,
        keep_blanks=args.keep_blanks,
        from_images=from_images,
        keep_images=keep_images,
        output=_resolve_output(args),
        lineart_threshold=args.lineart_threshold,
    )


def _list_devices(events: EventWriter, *, as_json: bool) -> int:
    """Handle ``--list-devices`` and return the exit code."""
    require_tools(["scanimage"])
    devices = list_devices()
    if as_json:
        events.emit("devices", devices=devices)
    elif not devices:
        LOGGER.info("No scanner devices found.")
    else:
        for device in devices:
            marker = "" if is_real_device(device) else "  [virtual]"
            LOGGER.info(
                "%s  (%s %s, %s)%s",
                device["device"],
                device["vendor"],
                device["model"],
                device["type"],
                marker,
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ScanMole command line and return a process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    _install_sigterm_handler()
    events = EventWriter(enabled=args.json)
    output: Path | None = None
    try:
        if args.list_devices:
            return _list_devices(events, as_json=args.json)
        config = _build_config(args)
        output = config.output
        return run_pipeline(config, events)
    except ScanMoleError as exc:
        events.error(exc.message, code=exc.exit_code)
        LOGGER.error("%s", exc.message)
        return exc.exit_code
    except subprocess.TimeoutExpired as exc:
        message = f"command timed out: {_format_command(exc.cmd)}"
        events.error(message, code=3)
        LOGGER.error("%s", message)
        return 3
    except KeyboardInterrupt:
        events.error("interrupted", code=_INTERRUPTED_EXIT_CODE)
        LOGGER.error("interrupted")
        return _INTERRUPTED_EXIT_CODE
    except _Terminated:
        events.error("terminated", code=_TERMINATED_EXIT_CODE)
        LOGGER.error("terminated")
        return _TERMINATED_EXIT_CODE
    except Exception as exc:  # process boundary: keep the JSON error contract
        message = f"unexpected error: {type(exc).__name__}: {exc}"
        events.error(message, code=1)
        LOGGER.error("%s", message)
        return 1
    finally:
        # Covers every failure and interrupt path above; after a successful
        # publish the file has content and is left alone.
        _discard_unused_reservation(output)


def _format_command(cmd: object) -> str:
    """Render a subprocess command (list or string) for an error message."""
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
