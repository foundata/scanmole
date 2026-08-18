"""Tests for the GUI's scan-mode table and argv mapping (no GTK needed)."""

from __future__ import annotations

from scanmole_gui.modes import SCAN_MODES, mode_argv


def test_scan_modes_cover_the_cli_modes_plus_faint() -> None:
    values = [value for _label, value in SCAN_MODES]

    assert values == ["lineart", "gray", "color", "lineart-auto"]


def test_mode_argv_maps_faint_to_the_auto_threshold() -> None:
    assert mode_argv("lineart-auto") == [
        "--mode",
        "lineart",
        "--lineart-threshold",
        "auto",
    ]


def test_mode_argv_plain_modes_omit_the_threshold_option() -> None:
    # Plain B/W inherits the CLI default; gray and color pass through.
    assert mode_argv("lineart") == ["--mode", "lineart"]
    assert mode_argv("gray") == ["--mode", "gray"]
    assert mode_argv("color") == ["--mode", "color"]
