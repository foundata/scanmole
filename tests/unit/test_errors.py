"""Tests that each domain error carries its documented exit code."""

from __future__ import annotations

from scanmole.errors import (
    DeviceError,
    InputError,
    MissingDependencyError,
    NoPagesError,
    ScanMoleError,
)


def test_exit_codes_match_contract() -> None:
    assert ScanMoleError("x").exit_code == 1
    assert InputError("x").exit_code == 2
    assert NoPagesError("x").exit_code == 2
    assert DeviceError("x").exit_code == 3
    assert MissingDependencyError("x").exit_code == 4


def test_message_is_preserved() -> None:
    error = DeviceError("scanner offline")

    assert error.message == "scanner offline"
    assert str(error) == "scanner offline"
