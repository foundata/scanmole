"""Command-line interface for ScanMole.

Builds a :class:`~scanmole.config.ScanConfig` from parsed arguments, configures
logging (diagnostics to stderr) and runs the pipeline. Machine-readable JSON
events go to stdout via :class:`~scanmole.events.EventWriter`.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import override

from scanmole import __version__
from scanmole.config import ScanConfig
from scanmole.devices import DEVICE_ENV_VAR, is_real_device, list_devices
from scanmole.errors import InputError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.external import require_tools
from scanmole.pipeline import run_pipeline

LOGGER = logging.getLogger("scanmole")

_INTERRUPTED_EXIT_CODE = 130


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
            "./YYYY-MM-DD_scan_HH-MM.pdf\n"
            "  scanmole invoice               -> ./invoice.pdf\n"
            "  scanmole --source flatbed --mode gray -r 150 --no-ocr -o test.pdf\n"
            "  scanmole --from-images p1.png p2.png -o doc.pdf\n"
            "  scanmole --list-devices --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "outbase",
        nargs="?",
        metavar="OUTBASE",
        help="output file base name (.pdf appended if missing)",
    )
    parser.add_argument("-o", "--output", metavar="FILE", help="output PDF file")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list SANE devices and exit",
    )
    parser.add_argument(
        "-d",
        "--device",
        default=os.environ.get(DEVICE_ENV_VAR) or None,
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
        default="a4",
        metavar="SIZE",
        help="a4|a5|a6|letter|legal or WxH in mm (default: %(default)s)",
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
        default="deu",
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
        action="store_true",
        help="produce PDF/A instead of plain PDF",
    )
    parser.add_argument(
        "--blank-threshold",
        type=float,
        default=0.995,
        metavar="F",
        help="mean brightness above which a page is blank (default: %(default)s)",
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
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _unique_output(path: Path) -> Path:
    """Return ``path`` or the next free ``name_2.pdf``, ``name_3.pdf``, ..."""
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _resolve_output(args: argparse.Namespace) -> Path:
    """Resolve the final, non-overwriting output path from the arguments.

    Raises:
        InputError: If both ``-o`` and a positional base name are given.
    """
    if args.output and args.outbase:
        raise InputError("give either -o/--output or a positional OUTBASE, not both")
    name = args.output or args.outbase
    if not name:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_scan_%H-%M")
        name = f"{stamp}.pdf"
    path = Path(name).expanduser()
    if path.suffix.lower() != ".pdf":
        path = path.with_name(path.name + ".pdf")
    return _unique_output(path.resolve())


def _build_config(args: argparse.Namespace) -> ScanConfig:
    """Translate parsed arguments into a :class:`ScanConfig`."""
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
    events = EventWriter(enabled=args.json)
    try:
        if args.list_devices:
            return _list_devices(events, as_json=args.json)
        return run_pipeline(_build_config(args), events)
    except ScanMoleError as exc:
        events.error(exc.message)
        LOGGER.error("%s", exc.message)
        return exc.exit_code
    except subprocess.TimeoutExpired as exc:
        message = f"command timed out: {_format_command(exc.cmd)}"
        events.error(message)
        LOGGER.error("%s", message)
        return 3
    except KeyboardInterrupt:
        events.error("interrupted")
        LOGGER.error("interrupted")
        return _INTERRUPTED_EXIT_CODE
    except Exception as exc:  # process boundary: keep the JSON error contract
        message = f"unexpected error: {type(exc).__name__}: {exc}"
        events.error(message)
        LOGGER.error("%s", message)
        return 1


def _format_command(cmd: object) -> str:
    """Render a subprocess command (list or string) for an error message."""
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
