"""Tests for device-option mapping (pure functions, no scanner needed)."""

from __future__ import annotations

import pytest

from scanmole.errors import DeviceError, InputError
from scanmole.options import (
    Capability,
    map_mode,
    map_source,
    parse_page_size,
    snap_resolution,
)


def _enum(*choices: str) -> Capability:
    return Capability(kind="enum", choices=list(choices))


def test_map_source_matches_brother_style_duplex() -> None:
    caps = {"source": _enum("Automatic Document Feeder(left aligned)", "ADF Duplex")}

    assert map_source("adf-duplex", caps) == "ADF Duplex"


def test_map_source_flatbed_ignores_spacing() -> None:
    caps = {"source": _enum("ADF", "Flat bed")}

    assert map_source("flatbed", caps) == "Flat bed"


def test_map_source_returns_none_without_source_option() -> None:
    assert map_source("adf", {}) is None


def test_map_source_raises_when_no_choice_matches() -> None:
    caps = {"source": _enum("ADF", "ADF Duplex")}

    with pytest.raises(DeviceError, match="no source matching 'flatbed'"):
        map_source("flatbed", caps)


def test_map_mode_falls_back_when_lineart_absent() -> None:
    # eSCL/airscan devices commonly offer only Color and Gray.
    caps = {"mode": _enum("Color", "Gray")}

    assert map_mode("lineart", caps) == "Gray"


def test_map_mode_prefers_exact_request() -> None:
    caps = {"mode": _enum("Lineart", "Gray", "Color")}

    assert map_mode("color", caps) == "Color"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("a4", (210.0, 297.0)),
        ("A4", (210.0, 297.0)),
        ("legal", (215.9, 355.6)),
        ("210x297", (210.0, 297.0)),
        ("148.5x210", (148.5, 210.0)),
    ],
)
def test_parse_page_size_accepts_names_and_dimensions(
    spec: str, expected: tuple[float, float]
) -> None:
    assert parse_page_size(spec) == expected


def test_parse_page_size_rejects_garbage() -> None:
    with pytest.raises(InputError, match="invalid --page-size"):
        parse_page_size("huge")


def test_snap_resolution_picks_nearest_enum_value() -> None:
    caps = {"resolution": _enum("75", "150", "300", "600")}

    assert snap_resolution(400, caps) == 300


def test_snap_resolution_clamps_to_range_maximum() -> None:
    caps = {"resolution": Capability(kind="range", minimum=75.0, maximum=600.0)}

    assert snap_resolution(1200, caps) == 600


def test_snap_resolution_none_without_option() -> None:
    assert snap_resolution(300, {}) is None
