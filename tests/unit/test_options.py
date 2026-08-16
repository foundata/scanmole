"""Tests for device-option mapping (pure functions, no scanner needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scanmole.errors import DeviceError, InputError
from scanmole.options import (
    Capability,
    map_mode,
    map_source,
    parse_capabilities,
    parse_page_size,
    probe_capabilities,
    snap_resolution,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanimage-A"


def _fixture_caps(name: str) -> dict[str, Capability]:
    return parse_capabilities((FIXTURES / name).read_text())


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


def test_parse_page_size_auto_returns_none() -> None:
    assert parse_page_size("auto") is None
    assert parse_page_size(" AUTO ") is None


def test_snap_resolution_picks_nearest_enum_value() -> None:
    caps = {"resolution": _enum("75", "150", "300", "600")}

    assert snap_resolution(400, caps) == 300


def test_snap_resolution_clamps_to_range_maximum() -> None:
    caps = {"resolution": Capability(kind="range", minimum=75.0, maximum=600.0)}

    assert snap_resolution(1200, caps) == 600


def test_snap_resolution_none_without_option() -> None:
    assert snap_resolution(300, {}) is None


# ---- fixture-pinned parsing and mapping (see tests/fixtures/scanimage-A/) ---


def test_fujitsu_fixture_maps_the_reference_settings() -> None:
    caps = _fixture_caps("fujitsu-scansnap-ix500.txt")

    assert map_source("adf-duplex", caps) == "ADF Duplex"
    assert map_mode("lineart", caps) == "Lineart"
    assert snap_resolution(300, caps) == 300
    assert caps["swdeskew"].kind == "bool"
    assert caps["swdespeck"].kind == "range"
    assert caps["swdespeck"].maximum == 9
    assert caps["page-width"].kind == "range"
    assert caps["page-width"].maximum == pytest.approx(221.121)


def test_brother_fixture_maps_long_source_and_mode_strings() -> None:
    caps = _fixture_caps("brother-brscan4.txt")

    assert (
        map_source("adf-duplex", caps)
        == "Automatic Document Feeder(left aligned,Duplex)"
    )
    assert map_mode("lineart", caps) == "Black & White"
    assert map_mode("gray", caps) == "True Gray"
    assert map_mode("color", caps) == "24bit Color"
    assert snap_resolution(300, caps) == 300
    assert snap_resolution(240, caps) == 200


def test_brother_fixture_keeps_bracketed_choices_intact() -> None:
    caps = _fixture_caps("brother-brscan4.txt")

    assert "24bit Color[Fast]" in caps["mode"].choices
    assert "24bit Color[Fast]]" not in caps["mode"].choices


def test_airscan_fixture_degrades_lineart_to_gray() -> None:
    caps = _fixture_caps("sane-airscan-escl.txt")

    assert map_mode("lineart", caps) == "Gray"
    assert map_source("adf-duplex", caps) == "ADF Duplex"
    assert snap_resolution(300, caps) == 300


def test_airscan_fixture_windows_differ_per_source() -> None:
    # eSCL devices advertise the scan window of the *selected* source; the
    # bare listing (simplex ADF) and the duplex listing disagree wildly, so
    # geometry must come from a probe with the mapped source applied.
    bare = _fixture_caps("sane-airscan-escl.txt")
    duplex = _fixture_caps("sane-airscan-escl-adf-duplex.txt")

    assert bare["y"].maximum == pytest.approx(3098.8)
    assert duplex["y"].maximum == pytest.approx(355.6)
    assert map_source("adf-duplex", duplex) == "ADF Duplex"


def test_probe_capabilities_applies_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(
        command: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("scanmole.options.run_command", fake_run)

    probe_capabilities("test:0", source="ADF Duplex")
    probe_capabilities("test:0")

    assert seen[0] == ["scanimage", "-d", "test:0", "--source", "ADF Duplex", "-A"]
    assert seen[1] == ["scanimage", "-d", "test:0", "-A"]


def test_sane_test_backend_fixture_has_no_duplex_source() -> None:
    caps = _fixture_caps("sane-test.txt")

    assert map_source("adf", caps) == "Automatic Document Feeder"
    assert map_mode("gray", caps) == "Gray"
    with pytest.raises(DeviceError, match="no source matching"):
        map_source("adf-duplex", caps)
