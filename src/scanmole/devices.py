"""SANE device discovery via ``scanimage``."""

from __future__ import annotations

import logging
import os
from typing import TypedDict

from scanmole.errors import DeviceError
from scanmole.external import PROBE_TIMEOUT_SECONDS, run_command

LOGGER = logging.getLogger(__name__)

DEVICE_ENV_VAR = "SCANMOLE_DEVICE"


class Device(TypedDict):
    """One SANE device as reported by ``scanimage``.

    The keys match the ``devices`` event payload exactly.
    """

    device: str
    vendor: str
    model: str
    type: str


def list_devices() -> list[Device]:
    """Return all SANE devices, parsed from ``scanimage -f``.

    Includes virtual devices (webcams, the SANE test backend); use
    :func:`is_real_device` to filter them.
    """
    result = run_command(
        ["scanimage", "-f", "%d|%v|%m|%t%n"],
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    devices: list[Device] = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 4 or not parts[0].strip():
            continue
        devices.append(
            Device(
                device=parts[0].strip(),
                vendor=parts[1].strip(),
                model=parts[2].strip(),
                type="|".join(parts[3:]).strip(),
            )
        )
    if not devices and result.returncode != 0:
        LOGGER.debug(
            "scanimage -f exited %d: %s", result.returncode, result.stderr.strip()
        )
    return devices


def is_real_device(device: Device) -> bool:
    """Return whether a device is a real scanner, not a webcam or test backend."""
    return not device["device"].startswith(("v4l:", "test:")) and (
        "virtual" not in device["type"].lower()
    )


def pick_default_device() -> str:
    """Return the device to use when none was given on the command line.

    Prefers the ``SCANMOLE_DEVICE`` environment variable, otherwise the first
    real device reported by SANE.

    Raises:
        DeviceError: If no real scanner can be found.
    """
    from_env = os.environ.get(DEVICE_ENV_VAR)
    if from_env:
        return from_env
    real = [device for device in list_devices() if is_real_device(device)]
    if not real:
        raise DeviceError(
            "no scanner device found (is it connected and powered on? check "
            f"with `scanimage -L`); or set -d/--device or {DEVICE_ENV_VAR}"
        )
    chosen = real[0]
    LOGGER.debug(
        "auto-selected device: %s (%s %s)",
        chosen["device"],
        chosen["vendor"],
        chosen["model"],
    )
    return chosen["device"]
