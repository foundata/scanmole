"""Hardware sensor evidence: did a scan button latch, is paper loaded?

Recognition is deliberately narrow and evidence-based. Only the exact
active boolean capabilities named ``scan`` and ``page-loaded`` count, and
only when their current value parses as ``yes`` or ``no``. Everything else
(a missing option, an inactive one, a non-boolean kind, an unparseable
value, a similarly named option) is *unavailable* evidence, never
``False``. Device identities are never consulted, and behavior measured on
one model (a latching button) is never generalized to another: consumers
must stay one-trigger-at-a-time even if a backend keeps ``scan=yes``
across several reads.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scanmole.errors import DeviceError
from scanmole.options import Capability, probe_capabilities

LOGGER = logging.getLogger(__name__)

SENSOR_PROBE_TIMEOUT_SECONDS = 15.0
"""Timeout for one sensor read (a capability listing with settings applied).

A read costs well under a second on measured hardware; a wedged backend
must not stall a wait loop for the full probe timeout. Cleanup of a hung
read is :func:`~scanmole.external.run_command`'s ordinary process-group
termination."""


@dataclass(frozen=True)
class SensorSnapshot:
    """One observation of the hardware sensors; ``None`` means unavailable."""

    scan: bool | None = None
    """The scan-button state, where the device exposes one."""
    page_loaded: bool | None = None
    """The paper-presence level, where the device exposes one."""

    @property
    def usable(self) -> bool:
        """Whether the observation carries any sensor evidence at all."""
        return self.scan is not None or self.page_loaded is not None


def _sensor_value(caps: dict[str, Capability], name: str) -> bool | None:
    capability = caps.get(name)
    if capability is None or not capability.active or capability.kind != "bool":
        return None
    if capability.current == "yes":
        return True
    if capability.current == "no":
        return False
    return None


def assess_sensors(caps: dict[str, Capability]) -> SensorSnapshot:
    """Pure sensor assessment of one capability snapshot."""
    return SensorSnapshot(
        scan=_sensor_value(caps, "scan"),
        page_loaded=_sensor_value(caps, "page-loaded"),
    )


def probe_sensors(
    device: str,
    settings: Sequence[tuple[str, str]] = (),
    timeout_seconds: float = SENSOR_PROBE_TIMEOUT_SECONDS,
    on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> SensorSnapshot:
    """Read the sensors once; any probe failure is unavailable evidence.

    ``settings`` should carry the complete final acquisition settings when
    a valid source-dependent snapshot is required. Sensor options are
    read-only state: they are parsed from the listing and never emitted
    back to scanimage. Interrupts (SIGINT, the CLI's SIGTERM translation)
    propagate; only probe failures collapse to an unavailable snapshot.
    """
    try:
        caps = probe_capabilities(device, settings, timeout_seconds, on_spawn)
    except (DeviceError, subprocess.SubprocessError, OSError) as exc:
        LOGGER.debug("sensor read of %s failed: %s", device, exc)
        return SensorSnapshot()
    return assess_sensors(caps)
