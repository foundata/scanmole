"""Tests for capability negotiation, pinned against the real -A fixtures."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from scanmole.errors import DeviceError
from scanmole.negotiation import (
    Support,
    assess_mode,
    assess_source,
    choice_support,
    log_notices,
    negotiate,
    probe_snapshot,
    require_supported,
)
from scanmole.options import Capability, parse_capabilities

FIXTURES = Path(__file__).parent.parent / "fixtures" / "scanimage-A"


def _fixture(name: str) -> dict[str, Capability]:
    return parse_capabilities((FIXTURES / name).read_text())


def _enum(*choices: str) -> Capability:
    return Capability(kind="enum", choices=list(choices))


# ---- default request per fixture: the whole fleet, pinned -----------------


def test_ix500_default_request_is_fully_native() -> None:
    plan = negotiate(
        _fixture("fujitsu-scansnap-ix500.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )

    assert plan.source.support is Support.NATIVE
    assert plan.source.backend_value == "ADF Duplex"
    assert plan.mode.support is Support.NATIVE
    assert plan.mode.backend_value == "Lineart"
    assert plan.depth.effective == "1"
    assert plan.resolution.support is Support.NATIVE


def test_ix100_duplex_degrades_to_the_front_side() -> None:
    plan = negotiate(
        _fixture("fujitsu-scansnap-ix100.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )

    assert plan.source.support is Support.DEGRADED
    assert plan.source.backend_value == "ADF Front"
    assert plan.source.effective == "adf"
    assert "backs will not be scanned" in plan.source.consequence


def test_brother_default_request_is_native() -> None:
    plan = negotiate(
        _fixture("brother-brscan4.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )

    assert plan.source.support is Support.NATIVE
    assert plan.mode.support is Support.NATIVE
    assert plan.mode.backend_value == "Black & White"


def test_escl_lineart_is_emulated_in_software() -> None:
    plan = negotiate(
        _fixture("sane-airscan-escl.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )

    assert plan.source.support is Support.NATIVE
    assert plan.mode.support is Support.EMULATED
    assert plan.mode.backend_value == "Gray"
    assert plan.mode.effective == "lineart"  # semantics preserved
    assert plan.depth.support is Support.EMULATED


def test_escl_lineart_without_conversion_is_degraded() -> None:
    plan = negotiate(
        _fixture("sane-airscan-escl.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
        lineart_threshold=0,
    )

    assert plan.mode.support is Support.DEGRADED
    assert plan.mode.reason == "conversion-disabled"


def test_canon_flatbed_only_device() -> None:
    caps = _fixture("canon-lide220-genesys.txt")
    plan = negotiate(caps, source="adf-duplex", mode="lineart", resolution=300)

    assert plan.source.support is Support.DEGRADED
    assert plan.source.effective == "flatbed"
    assert plan.mode.support is Support.EMULATED  # only Gray/Color offered
    assert assess_source(caps, "flatbed").support is Support.NATIVE


def test_sane_test_backend_duplex_degrades_to_simplex() -> None:
    plan = negotiate(
        _fixture("sane-test.txt"), source="adf-duplex", mode="gray", resolution=300
    )

    assert plan.source.support is Support.DEGRADED
    assert plan.source.backend_value == "Automatic Document Feeder"
    assert plan.mode.support is Support.NATIVE


def test_epson2_misdetection_keeps_the_source_unknown() -> None:
    # The epson2 backend lists an inactive Flatbed source on the sheet-fed
    # DS-730N: inactive evidence must stay UNKNOWN, never UNSUPPORTED.
    plan = negotiate(
        _fixture("epson-ds730n-epson2.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )

    assert plan.source.support is Support.UNKNOWN
    assert plan.source.reason == "source-option-inactive"
    assert plan.source.backend_value is None  # never passed to the command


def test_epsonds_faint_mode_is_degraded_by_native_lineart() -> None:
    plan = negotiate(
        _fixture("epson-ds730n-epsonds.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
        lineart_threshold="auto",
    )

    assert plan.mode.requested == "lineart-auto"
    assert plan.mode.support is Support.DEGRADED
    assert plan.mode.reason == "native-1bit-defeats-adaptive"
    assert plan.mode.backend_value == "Lineart"


def test_escl_faint_mode_is_emulated() -> None:
    assessment = assess_mode(_fixture("sane-airscan-escl.txt"), "lineart-auto", "auto")

    assert assessment.support is Support.EMULATED


# ---- evidence classes: absent, inactive, active-but-nonmatching -----------


def test_absent_source_option_is_unknown() -> None:
    assessment = assess_source({}, "adf-duplex")

    assert assessment.support is Support.UNKNOWN
    assert assessment.reason == "no-source-option"
    assert assessment.effective == "adf-duplex"  # best-effort as requested


def test_inactive_source_option_is_unknown_not_unsupported() -> None:
    caps = {"source": Capability(kind="enum", choices=["Flatbed"], active=False)}

    assessment = assess_source(caps, "flatbed")

    assert assessment.support is Support.UNKNOWN
    assert assessment.reason == "source-option-inactive"


def test_active_enum_without_a_flatbed_is_unsupported() -> None:
    caps = {"source": _enum("ADF Front", "ADF Duplex")}

    assessment = assess_source(caps, "flatbed")

    assert assessment.support is Support.UNSUPPORTED
    assert "no source matching" in assessment.consequence


def test_probe_failure_yields_unknown_throughout() -> None:
    plan = negotiate(None, source="adf-duplex", mode="lineart", resolution=300)

    assert plan.source.support is Support.UNKNOWN
    assert plan.mode.support is Support.UNKNOWN
    assert plan.depth.support is Support.UNKNOWN
    assert plan.resolution.support is Support.UNKNOWN


# ---- exactness rules ------------------------------------------------------


def test_duplex_choice_is_not_an_exact_simplex_match() -> None:
    caps = {"source": _enum("ADF Duplex")}

    assessment = assess_source(caps, "adf")

    assert assessment.support is Support.DEGRADED
    assert assessment.reason == "only-duplex-feeder"
    assert assessment.effective == "adf-duplex"
    assert "back sides will also be scanned" in assessment.consequence


def test_back_request_falls_back_to_the_front_side() -> None:
    caps = {"source": _enum("ADF Front")}

    assessment = assess_source(caps, "adf-back")

    assert assessment.support is Support.DEGRADED
    assert "front sides" in assessment.consequence


def test_gray_and_color_fallbacks_name_their_losses() -> None:
    only_gray = {"mode": _enum("Gray")}
    only_color = {"mode": _enum("Color")}

    color_on_gray = assess_mode(only_gray, "color")
    gray_on_color = assess_mode(only_color, "gray")

    assert color_on_gray.support is Support.DEGRADED
    assert "color will be lost" in color_on_gray.consequence
    assert gray_on_color.support is Support.DEGRADED
    assert "larger files" in gray_on_color.consequence


def test_resolution_snapping_is_degraded_but_usable() -> None:
    plan = negotiate(
        _fixture("brother-brscan4.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=240,
    )

    assert plan.resolution.support is Support.DEGRADED
    assert plan.resolution.effective == "200"
    assert "instead of 240 dpi" in plan.resolution.consequence


# ---- notices and errors ---------------------------------------------------


def test_notices_warn_once_per_consequence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = negotiate(
        _fixture("fujitsu-scansnap-ix100.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )
    logger = logging.getLogger("test-negotiation")

    with caplog.at_level(logging.INFO, logger="test-negotiation"):
        log_notices(plan, logger)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # one degraded setting, exactly one warning
    assert "backs will not be scanned" in warnings[0].message


def test_emulated_paths_log_at_info_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = negotiate(
        _fixture("sane-airscan-escl.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
    )
    logger = logging.getLogger("test-negotiation")

    with caplog.at_level(logging.INFO, logger="test-negotiation"):
        log_notices(plan, logger)

    assert all(r.levelno < logging.WARNING for r in caplog.records)
    assert any("software" in r.message for r in caplog.records)


def test_unsupported_raises_the_established_device_error() -> None:
    plan = negotiate(
        _fixture("fujitsu-scansnap-ix500.txt"),
        source="flatbed",
        mode="lineart",
        resolution=300,
    )

    with pytest.raises(DeviceError, match="no source matching 'flatbed'"):
        require_supported(plan)


# ---- frontend helpers -----------------------------------------------------


def test_choice_support_covers_every_gui_choice() -> None:
    support = choice_support(_fixture("fujitsu-scansnap-ix500.txt"))

    assert support.sources["adf-duplex"] is Support.NATIVE
    assert support.sources["adf"] is Support.NATIVE  # ADF Front is exact
    assert support.sources["flatbed"] is Support.UNSUPPORTED
    assert support.modes["lineart"] is Support.NATIVE
    assert support.modes["lineart-auto"] is Support.DEGRADED


def test_choice_support_with_failed_probe_is_all_unknown() -> None:
    support = choice_support(None)

    assert set(support.sources.values()) == {Support.UNKNOWN}
    assert set(support.modes.values()) == {Support.UNKNOWN}


def test_probe_snapshot_turns_failures_into_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(command: list[str], timeout_seconds: float) -> None:
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    monkeypatch.setattr("scanmole.options.run_command", failing)

    assert probe_snapshot("test:0") is None
