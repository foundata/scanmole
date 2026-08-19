"""Device discovery result handling (no GTK).

Owns the pure half of the device search: parsing the CLI's
``--list-devices --json`` event stream and its ``--version`` output,
filtering virtual devices, and the compatibility decision against the
``hello`` handshake. The window keeps command execution (through the
supervised engine helper), threads, GLib scheduling, translations and
the retry/poll timers; this module only turns raw output into one typed
result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from scanmole_gui import __version__, incompatible_cli

_VIRTUAL_PREFIXES = ("v4l:", "test:")
"""Hidden device classes: webcams and the SANE test backend."""


@dataclass(frozen=True)
class Listing:
    """The decided outcome of one device search.

    ``needed`` carries the version requirement text when the CLI cannot
    be driven (the device list is empty then, whatever the CLI printed);
    ``failed_exit`` carries a nonzero exit code that produced no usable
    devices. ``retry`` says whether automatic polling makes sense: only
    while nothing usable was found and the CLI itself is compatible.
    """

    devices: list[dict[str, str]] = field(default_factory=list)
    cli_version: str | None = None
    needed: str | None = None
    failed_exit: int | None = None

    @property
    def retry(self) -> bool:
        return not self.devices and self.needed is None


def _clean_device(entry: object) -> dict[str, str] | None:
    """One sanitized device entry, or ``None`` when unusable.

    Only string-valued fields survive: the GUI reads ``vendor`` and
    ``model`` as strings, and a wrong-typed value from a foreign or
    hand-rolled producer must degrade to a missing field instead of
    crashing the name rendering later. An entry without a nonempty
    string ``device`` identifier is dropped entirely: it would count as
    a found scanner, yet selecting it yields no identifier and a scan
    would fall through to whatever default device the CLI picks.
    """
    if not isinstance(entry, dict):
        return None
    cleaned = {
        key: value
        for key, value in entry.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    identifier = cleaned.get("device")
    if not identifier or identifier.startswith(_VIRTUAL_PREFIXES):
        return None
    return cleaned


def parse_listing(stdout: str) -> tuple[str | None, list[dict[str, str]]]:
    """Extract the hello version and real devices from the event stream.

    Tolerant on purpose: non-JSON lines and wrong-shaped values are
    skipped, virtual devices are hidden, and a malformed devices event
    (entries that are not objects, a payload that is not a list) is
    dropped without erasing an earlier valid one.
    """
    hello_version: str | None = None
    devices: list[dict[str, str]] = []
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue  # valid JSON, wrong shape: not ours to crash on
        if event.get("event") == "hello":
            hello_version = str(event.get("version") or "") or None
        if event.get("event") == "devices":
            payload = event.get("devices")
            if not isinstance(payload, list):
                continue
            devices = [
                cleaned
                for cleaned in (_clean_device(entry) for entry in payload)
                if cleaned is not None
            ]
    return hello_version, devices


def evaluate_listing(stdout: str, returncode: int) -> Listing:
    """Turn one ``--list-devices --json`` run into a decided result.

    A CLI whose version cannot be driven is refused instead of guessed
    at: a mismatched protocol rendering silently wrong state is worse
    than a hard stop. A failing run without a ``hello`` event predates
    the handshake protocol and is reported as a plain search failure.
    """
    hello_version, devices = parse_listing(stdout)
    needed = (
        incompatible_cli(__version__, hello_version)
        if hello_version is not None or returncode == 0
        else None
    )
    if needed is not None:
        return Listing(devices=[], cli_version=hello_version, needed=needed)
    failed_exit = returncode if returncode != 0 and not devices else None
    return Listing(devices=devices, cli_version=hello_version, failed_exit=failed_exit)


def parse_version(stdout: str) -> str | None:
    """The version from ``scanmole --version`` output, or ``None``."""
    lines = stdout.strip().splitlines()
    parts = lines[0].split() if lines else []
    return parts[-1] if len(parts) >= 2 else None


def display_name(device: dict[str, str], fallback: str) -> str:
    """The human name for one device entry (vendor and model, or the id)."""
    vendor = (device.get("vendor") or "").strip()
    model = (device.get("model") or "").strip()
    return " ".join(part for part in (vendor, model) if part) or device.get(
        "device", fallback
    )
