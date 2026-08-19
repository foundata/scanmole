"""Tests for capability negotiation, pinned against the real -A fixtures."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from scanmole.errors import DeviceError
from scanmole.negotiation import (
    Plan,
    Support,
    advisory_faint_assessment,
    assess_mode,
    assess_resolution,
    assess_source,
    choice_support,
    detect_native_enhancement,
    log_notices,
    negotiate,
    probe_snapshot,
    require_supported,
    resolve_faint_plan,
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


def test_epsonds_faint_mode_acquires_gray_with_pinned_depth() -> None:
    # No native enhancement on the epsonds DS-730N: the faint request must
    # not settle on the device's plain Lineart, but acquire Gray at an
    # explicit 8 bit for the guarded adaptive conversion.
    plan = negotiate(
        _fixture("epson-ds730n-epsonds.txt"),
        source="adf-duplex",
        mode="lineart",
        resolution=300,
        lineart_threshold="auto",
    )

    assert plan.mode.requested == "lineart-auto"
    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-gray"
    assert plan.mode.backend_value == "Gray"
    assert plan.depth.backend_value == "8"  # --depth 1|8bit is active


def test_escl_faint_mode_is_emulated() -> None:
    assessment = assess_mode(_fixture("sane-airscan-escl.txt"), "lineart-auto", "auto")

    assert assessment.support is Support.EMULATED
    assert assessment.reason == "adaptive-gray"


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


# ---- resolution evidence --------------------------------------------------


def test_fixed_single_choice_resolution_snaps_and_is_emitted() -> None:
    caps = {"resolution": Capability(kind="enum", choices=["200dpi"], current="200")}

    assessment = assess_resolution(caps, 300)

    assert assessment.support is Support.DEGRADED
    assert assessment.backend_value == "200"
    assert assessment.effective == "200"


def test_inactive_resolution_with_a_readable_value_establishes_the_dpi() -> None:
    caps = {"resolution": Capability(kind="enum", choices=["200dpi"], active=False)}

    degraded = assess_resolution(caps, 300)
    matching = assess_resolution(caps, 200)

    assert degraded.support is Support.DEGRADED
    assert degraded.reason == "fixed-resolution"
    assert degraded.backend_value is None  # never emitted for inactive options
    assert degraded.effective == "200"
    assert "fixed at 200 dpi" in degraded.consequence
    assert matching.support is Support.NATIVE
    assert matching.effective == "200"


def test_unknown_resolution_never_fakes_an_effective_value() -> None:
    absent = assess_resolution({}, 300)
    inactive_unreadable = assess_resolution(
        {"resolution": Capability(kind="range", minimum=0, maximum=0, active=False)},
        300,
    )

    assert absent.support is Support.UNKNOWN and absent.effective == ""
    assert inactive_unreadable.support is Support.UNKNOWN
    assert inactive_unreadable.effective == ""


def test_stepped_range_resolution_snaps_with_lower_ties() -> None:
    caps = {
        "resolution": Capability(kind="range", minimum=100, maximum=600, step=100.0)
    }

    assessment = assess_resolution(caps, 250)

    assert assessment.support is Support.DEGRADED
    assert assessment.effective == "200"
    assert "200 dpi instead of 250 dpi" in assessment.consequence


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
    # Tentatively NATIVE: the SDTC signature (active --variance plus an
    # active --threshold range containing 0) is visible in the snapshot.
    assert support.modes["lineart-auto"] is Support.NATIVE


def test_choice_support_with_failed_probe_is_all_unknown() -> None:
    support = choice_support(None)

    assert set(support.sources.values()) == {Support.UNKNOWN}
    assert set(support.modes.values()) == {Support.UNKNOWN}


# ---- faint mode: native recognition, staged probes, fallbacks -------------


class _Prober:
    """A fake staged prober recording the ordered settings of every call."""

    def __init__(self, *results: dict[str, Capability] | None) -> None:
        self.calls: list[tuple[tuple[str, str], ...]] = []
        self._results = list(results)

    def __call__(
        self, settings: tuple[tuple[str, str], ...]
    ) -> dict[str, Capability] | None:
        self.calls.append(settings)
        return self._results.pop(0) if self._results else None


def _faint_plan(caps: dict[str, Capability] | None) -> Plan:
    return negotiate(
        caps,
        source="adf-duplex",
        mode="lineart",
        resolution=300,
        lineart_threshold="auto",
    )


def test_fujitsu_sdtc_is_recognized_with_ordered_set_and_reprobe() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    prober = _Prober(caps, caps)
    base = (("--source", "ADF Duplex"),)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober, base)

    assert plan.mode.support is Support.NATIVE
    assert plan.mode.reason == "native-fujitsu-sdtc"
    assert plan.mode.backend_value == "Lineart"
    assert plan.extra_options == (("--threshold", "0"), ("--variance", "0"))
    assert plan.depth.effective == "1"
    assert prober.calls == [
        (("--source", "ADF Duplex"), ("--mode", "Lineart")),
        (
            ("--source", "ADF Duplex"),
            ("--mode", "Lineart"),
            ("--threshold", "0"),
            ("--variance", "0"),
        ),
    ]


def test_ix100_carries_the_same_sdtc_evidence() -> None:
    caps = _fixture("fujitsu-scansnap-ix100.txt")

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps, caps))

    assert plan.mode.support is Support.NATIVE
    assert plan.mode.reason == "native-fujitsu-sdtc"


def test_epson_tet_is_recognized_without_a_verification_reprobe() -> None:
    caps = _fixture("epson-perfection1660-epson2.txt")
    prober = _Prober(caps)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober)

    assert plan.mode.support is Support.NATIVE
    assert plan.mode.reason == "native-epson-tet"
    assert plan.extra_options == (("--halftoning", "Text Enhanced Technology"),)
    assert prober.calls == [(("--mode", "Lineart"),)]  # source is inactive


def test_sdtc_is_rejected_when_variance_goes_inactive_on_reprobe() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    reprobed = _fixture("fujitsu-scansnap-ix500.txt")
    reprobed["variance"].active = False

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps, reprobed))

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-gray"
    assert plan.extra_options == ()


def test_failed_candidate_probe_falls_back_to_software() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(None))

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-gray"
    assert plan.mode.backend_value == "Gray"


def test_lineart_only_device_is_unsupported_with_guidance() -> None:
    caps = {"mode": _enum("Lineart")}
    prober = _Prober(caps)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober)

    assert prober.calls  # the candidate probe ran before the verdict
    assert plan.mode.support is Support.UNSUPPORTED
    assert plan.mode.reason == "no-information-preserving-path"
    assert "ordinary B/W" in plan.mode.consequence
    with pytest.raises(DeviceError, match="ordinary B/W"):
        require_supported(plan)


def test_faint_falls_back_to_color_when_gray_is_missing() -> None:
    assessment = assess_mode({"mode": _enum("Color", "Lineart")}, "lineart-auto")

    assert assessment.support is Support.EMULATED
    assert assessment.reason == "adaptive-color"
    assert assessment.backend_value == "Color"


def test_faint_with_inconclusive_capabilities_stays_unknown() -> None:
    absent = assess_mode({}, "lineart-auto")
    inactive = assess_mode(
        {"mode": Capability(kind="enum", choices=["Lineart"], active=False)},
        "lineart-auto",
    )

    assert absent.support is Support.UNKNOWN
    assert absent.reason == "no-mode-option"
    assert inactive.support is Support.UNKNOWN
    assert inactive.reason == "mode-option-inactive"


# ---- faint mode: signatures that must NOT count as native -----------------


def test_brother_error_diffusion_is_not_native_and_gray_is_true_gray() -> None:
    caps = _fixture("brother-brscan4.txt")

    assert detect_native_enhancement(caps) is None
    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))
    assert plan.mode.support is Support.EMULATED
    assert plan.mode.backend_value == "True Gray"  # never Gray[Error Diffusion]


def test_canon_halftone_mode_choice_is_not_native() -> None:
    caps = {"mode": _enum("Color", "Gray", "Halftone", "Lineart")}

    assert detect_native_enhancement(caps) is None
    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))
    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-gray"


def test_pixma_threshold_curve_is_not_native() -> None:
    caps = {
        "mode": _enum("Color", "Gray", "Lineart"),
        "threshold-curve": Capability(kind="range", minimum=0, maximum=127),
    }

    assert detect_native_enhancement(caps) is None


def test_generic_threshold_and_inactive_tet_are_not_native() -> None:
    # The epson2 DS-730N listing has an active generic --threshold and an
    # inactive --halftoning with the TET choice: neither is evidence.
    caps = _fixture("epson-ds730n-epson2.txt")

    assert detect_native_enhancement(caps) is None
    assert advisory_faint_assessment(caps).support is Support.EMULATED


def test_inactive_variance_is_not_native() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    caps["variance"].active = False

    assert detect_native_enhancement(caps) is None


def test_threshold_range_must_contain_zero() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    caps["threshold"].minimum = 1.0

    assert detect_native_enhancement(caps) is None


# ---- faint mode: advisory versus authoritative ----------------------------


def test_advisory_verdict_is_tentatively_native_on_visible_signature() -> None:
    assessment = advisory_faint_assessment(_fixture("fujitsu-scansnap-ix500.txt"))

    assert assessment.support is Support.NATIVE
    assert assessment.reason == "native-fujitsu-sdtc"


def test_negotiate_without_staged_probes_stays_command_safe() -> None:
    # Without the staged confirmation a plan must never select the plain
    # 1-bit mode for a faint request, signature or not.
    plan = _faint_plan(_fixture("fujitsu-scansnap-ix500.txt"))

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.backend_value == "Gray"


def test_scan_time_verdict_overrules_the_advisory_claim() -> None:
    # The GUI may have shown the choice as native; if the authoritative
    # set-and-reprobe cannot confirm it, the scan takes the software path.
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    staged = _fixture("fujitsu-scansnap-ix500.txt")
    staged["variance"].active = False

    advisory = advisory_faint_assessment(caps)
    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(staged))

    assert advisory.support is Support.NATIVE
    assert plan.mode.support is Support.EMULATED
    assert plan.extra_options == ()


def test_native_faint_plan_logs_one_info_notice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps, caps))
    logger = logging.getLogger("test-negotiation")

    with caplog.at_level(logging.INFO, logger="test-negotiation"):
        log_notices(plan, logger)

    notices = [r for r in caplog.records if "text enhancement" in r.message]
    assert len(notices) == 1
    assert all(r.levelno == logging.INFO for r in notices)


def test_probe_snapshot_turns_failures_into_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(command: list[str], timeout_seconds: float) -> None:
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    monkeypatch.setattr("scanmole.options.run_command", failing)

    assert probe_snapshot("test:0") is None
