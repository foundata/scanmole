"""Tests for SANE device discovery, with scanimage stubbed out."""

from __future__ import annotations

import subprocess

import pytest

from scanmole.devices import (
    Device,
    is_real_device,
    list_devices,
    pick_default_device,
)
from scanmole.errors import DeviceError


def _completed(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["scanimage"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_list_devices_raises_when_scanimage_fails_without_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed enumeration must not masquerade as "no scanners found":
    # scanimage exits 0 for a genuinely empty list, so nonzero plus nothing
    # parsed is an operational failure (access denied, broken backend).
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(
            "", returncode=1, stderr="scanimage: access denied"
        ),
    )

    with pytest.raises(DeviceError, match="access denied"):
        list_devices()


def test_list_devices_keeps_partial_output_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = "fujitsu:ScanSnap iX500:65535|FUJITSU|ScanSnap iX500|scanner\n"
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(
            listing, returncode=1, stderr="one backend crashed"
        ),
    )

    devices = list_devices()

    assert len(devices) == 1
    assert devices[0]["device"] == "fujitsu:ScanSnap iX500:65535"


def test_list_devices_empty_success_is_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(""),
    )

    assert list_devices() == []


def test_list_devices_parses_the_format_string_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        "fujitsu:ScanSnap iX500:65535|FUJITSU|ScanSnap iX500|scanner\n"
        "v4l:/dev/video0|Noname|Integrated Camera|virtual device\n"
        "\n"
        "broken line without pipes\n"
    )
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(listing),
    )

    devices = list_devices()

    assert devices == [
        Device(
            device="fujitsu:ScanSnap iX500:65535",
            vendor="FUJITSU",
            model="ScanSnap iX500",
            type="scanner",
        ),
        Device(
            device="v4l:/dev/video0",
            vendor="Noname",
            model="Integrated Camera",
            type="virtual device",
        ),
    ]


def test_is_real_device_filters_webcams_and_test_backend() -> None:
    real = Device(device="fujitsu:x", vendor="F", model="iX500", type="scanner")
    webcam = Device(device="v4l:/dev/video0", vendor="N", model="Cam", type="scanner")
    test = Device(device="test:0", vendor="Noname", model="frontend", type="scanner")
    virtual = Device(device="epson:x", vendor="E", model="X", type="virtual device")

    assert is_real_device(real) is True
    assert is_real_device(webcam) is False
    assert is_real_device(test) is False
    assert is_real_device(virtual) is False


def test_pick_default_device_prefers_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCANMOLE_DEVICE", "airscan:e0:Office")

    assert pick_default_device() == "airscan:e0:Office"


def test_pick_default_device_takes_the_first_real_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCANMOLE_DEVICE", raising=False)
    listing = (
        "v4l:/dev/video0|Noname|Cam|virtual device\n"
        "fujitsu:ScanSnap iX500:65535|FUJITSU|ScanSnap iX500|scanner\n"
    )
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(listing),
    )

    assert pick_default_device() == "fujitsu:ScanSnap iX500:65535"


def test_pick_default_device_fails_without_a_real_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty enumeration is a *success* (scanimage exits 0 then); only
    # webcams around means no scanner to pick.
    monkeypatch.delenv("SCANMOLE_DEVICE", raising=False)
    monkeypatch.setattr(
        "scanmole.devices.run_command",
        lambda command, timeout_seconds: _completed(
            "v4l:/dev/video0|Noname|Integrated Camera|virtual device\n"
        ),
    )

    with pytest.raises(DeviceError, match="no scanner device found"):
        pick_default_device()
