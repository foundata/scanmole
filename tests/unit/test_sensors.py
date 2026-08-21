"""Tests for the hardware sensor evidence layer (fixture-pinned, no hardware)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scanmole.errors import DeviceError
from scanmole.options import Capability, parse_capabilities
from scanmole.sensors import SensorSnapshot, assess_sensors, probe_sensors

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanimage-A"


def test_assess_sensors_reads_the_ix100_listing() -> None:
    listing = (_FIXTURES / "fujitsu-scansnap-ix100.txt").read_text()

    snapshot = assess_sensors(parse_capabilities(listing))

    assert snapshot == SensorSnapshot(scan=False, page_loaded=False)
    assert snapshot.usable


def test_assess_sensors_reports_a_latched_button_and_loaded_paper() -> None:
    caps = {
        "scan": Capability(kind="bool", current="yes"),
        "page-loaded": Capability(kind="bool", current="yes"),
    }

    assert assess_sensors(caps) == SensorSnapshot(scan=True, page_loaded=True)


def test_missing_sensors_are_unavailable_not_false() -> None:
    snapshot = assess_sensors({})

    assert snapshot == SensorSnapshot(scan=None, page_loaded=None)
    assert not snapshot.usable


def test_inactive_or_malformed_sensors_are_unavailable() -> None:
    caps = {
        "scan": Capability(kind="bool", current="yes", active=False),
        "page-loaded": Capability(kind="enum", choices=["yes", "no"], current="yes"),
    }

    assert assess_sensors(caps) == SensorSnapshot(scan=None, page_loaded=None)


def test_unparseable_sensor_values_are_unavailable() -> None:
    caps = {"scan": Capability(kind="bool", current="maybe")}

    assert assess_sensors(caps).scan is None


def test_fuzzy_option_names_are_never_sensors() -> None:
    # Only the exact names count; a "scan-area" or "pages-loaded" option is
    # not evidence of a button or a paper sensor.
    caps = {
        "scan-area": Capability(kind="bool", current="yes"),
        "pages-loaded": Capability(kind="bool", current="yes"),
    }

    assert not assess_sensors(caps).usable


def test_probe_sensors_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_probe(*args: object, **kwargs: object) -> dict[str, Capability]:
        raise DeviceError("open of device failed")

    monkeypatch.setattr("scanmole.sensors.probe_capabilities", failing_probe)

    assert probe_sensors("test:0") == SensorSnapshot()


def test_probe_sensors_applies_settings_and_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    def fake_probe(
        device: str,
        settings: tuple[tuple[str, str], ...] = (),
        timeout_seconds: float = 0.0,
        on_spawn: object = None,
    ) -> dict[str, Capability]:
        recorded.update(
            device=device, settings=tuple(settings), timeout=timeout_seconds
        )
        return {"scan": Capability(kind="bool", current="no")}

    monkeypatch.setattr("scanmole.sensors.probe_capabilities", fake_probe)

    snapshot = probe_sensors("test:0", (("--source", "ADF Front"),))

    assert snapshot.scan is False
    assert recorded["settings"] == (("--source", "ADF Front"),)
    assert isinstance(recorded["timeout"], float)
    assert 0 < recorded["timeout"] <= 30


def test_an_interrupt_kills_a_term_ignoring_sensor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hung or TERM-ignoring scanimage during a sensor read must not
    # survive a cancellation: the read runs under run_command's ordinary
    # process-group supervision, and the interrupt propagates.
    monkeypatch.setattr("scanmole.external.GROUP_KILL_GRACE_SECONDS", 0.3)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pid_file = tmp_path / "probe.pid"
    script = fake_bin / "scanimage"
    script.write_text(f"#!/bin/bash\ntrap '' TERM\necho $$ > {pid_file}\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    from scanmole.external import _PipeCapture

    real_pump = _PipeCapture.pump
    state = {"armed": True}

    def interrupting_pump(self: object, deadline: float) -> None:
        if state["armed"]:
            state["armed"] = False
            for _ in range(100):  # the child provably started
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            raise KeyboardInterrupt
        real_pump(self, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr("scanmole.external._PipeCapture.pump", interrupting_pump)

    with pytest.raises(KeyboardInterrupt):
        probe_sensors("test:0")

    pid = int(pid_file.read_text().strip())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the sensor read's child survived the interrupt")
