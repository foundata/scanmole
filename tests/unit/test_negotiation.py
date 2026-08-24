"""Tests for capability negotiation, pinned against the real -A fixtures.

Source, mode, depth and resolution are all assessed from one listing, and
the plan built from them carries the notices and the enforcement. The one
request that needs staged probes instead, ``lineart-auto``, has its own
module in ``test_negotiation_faint.py``.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from scanmole import assessment, faint, negotiation
from scanmole.errors import DeviceError
from scanmole.negotiation import (
    Support,
    assess_mode,
    assess_resolution,
    assess_source,
    choice_support,
    log_notices,
    negotiate,
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


def test_read_only_current_value_establishes_the_dpi() -> None:
    caps = parse_capabilities(
        "    --resolution <int> [300] [read-only]\n        Fixed scan resolution.\n"
    )

    degraded = assess_resolution(caps, 600)
    matching = assess_resolution(caps, 300)

    assert degraded.support is Support.DEGRADED
    assert degraded.reason == "fixed-resolution"
    assert degraded.backend_value is None  # read-only: never emitted
    assert degraded.effective == "300"
    assert matching.support is Support.NATIVE and matching.effective == "300"


def test_inactive_adjustable_range_stays_unknown() -> None:
    # 75..600dpi [75] [inactive]: adjustable when active, so its current
    # value proves nothing about what the backend would actually use.
    caps = parse_capabilities(
        "    --resolution 75..600dpi [75] [inactive]\n"
        "        Sets the resolution of the scanned image.\n"
    )

    assessment = assess_resolution(caps, 300)

    assert assessment.support is Support.UNKNOWN
    assert assessment.reason == "resolution-option-inactive"
    assert assessment.effective == ""


def test_opaque_or_nonnumeric_active_resolution_stays_unknown() -> None:
    opaque = {"resolution": Capability(kind="other", current="300")}
    words = {"resolution": Capability(kind="enum", choices=["draft", "best"])}

    for caps in (opaque, words):
        assessment = assess_resolution(caps, 300)
        assert assessment.support is Support.UNKNOWN
        assert assessment.reason == "resolution-not-parseable"
        assert assessment.backend_value is None and assessment.effective == ""


def test_genuinely_fixed_inactive_constraints_establish_the_dpi() -> None:
    singleton_enum = {
        "resolution": Capability(kind="enum", choices=["200dpi"], active=False)
    }
    equal_range = {
        "resolution": Capability(
            kind="range", minimum=300.0, maximum=300.0, active=False
        )
    }

    assert assess_resolution(singleton_enum, 300).effective == "200"
    assert assess_resolution(equal_range, 600).effective == "300"
    assert assess_resolution(equal_range, 600).support is Support.DEGRADED


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


# ---- read-only options are state, never something to emit ----------------


def _read_only(listing: str) -> dict[str, Capability]:
    return parse_capabilities(listing)


def test_read_only_source_matching_the_request_establishes_without_emitting() -> None:
    # A device fixed to ADF Front satisfies an ADF request, but the option
    # is not writable, so no --source may be produced for it.
    caps = _read_only("    --source ADF Front [ADF Front] [read-only]\n")

    assessment = assess_source(caps, "adf")

    assert assessment.support is Support.NATIVE
    assert assessment.effective == "adf"
    assert assessment.backend_value is None


def test_read_only_source_runs_the_ordinary_degradation_rules() -> None:
    # The fixed current value is matched by the same fallback rules as a
    # settable choice list, rather than collapsing to UNKNOWN.
    caps = _read_only("    --source ADF Front [ADF Front] [read-only]\n")

    duplex = assess_source(caps, "adf-duplex")
    assert duplex.support is Support.DEGRADED
    assert duplex.effective == "adf"
    assert duplex.backend_value is None

    flatbed = assess_source(caps, "flatbed")
    assert flatbed.support is Support.UNSUPPORTED
    assert flatbed.backend_value is None


def test_read_only_source_without_a_current_value_is_unknown() -> None:
    caps = _read_only("    --source ADF Front|Flatbed [read-only]\n")

    assessment = assess_source(caps, "adf")

    assert assessment.support is Support.UNKNOWN
    assert assessment.backend_value is None


def test_read_only_mode_matches_and_degrades_without_emitting() -> None:
    caps = _read_only("    --mode Lineart [Lineart] [read-only]\n")

    native = assess_mode(caps, "lineart", 0.5)
    assert native.support is Support.NATIVE
    assert native.effective == "lineart"
    assert native.backend_value is None

    colour = assess_mode(caps, "color", 0.5)
    assert colour.support is Support.DEGRADED
    assert colour.effective == "lineart"
    assert colour.backend_value is None


def test_writable_and_inactive_capabilities_keep_their_behaviour() -> None:
    writable = _read_only("    --source ADF Front|Flatbed [ADF Front]\n")
    assert assess_source(writable, "adf").backend_value == "ADF Front"

    inactive = _read_only("    --source ADF Front|Flatbed [inactive]\n")
    assessment = assess_source(inactive, "adf")
    assert assessment.support is Support.UNKNOWN
    assert assessment.backend_value is None


# ------------------------------------------------------- module boundary


def test_the_negotiation_surface_survives_the_split() -> None:
    # Three modules, one front door. The engine and both frontends have
    # always asked scanmole.negotiation what a device supports, and they
    # must not have to learn which of the three now answers.
    assert negotiation.Support is assessment.Support
    assert negotiation.Assessment is assessment.Assessment
    assert negotiation.Plan is assessment.Plan
    assert negotiation.resolve_faint_plan is faint.resolve_faint_plan
    assert negotiation.advisory_faint_assessment is faint.advisory_faint_assessment
    assert negotiation.detect_native_enhancement is faint.detect_native_enhancement
    for name in negotiation.__all__:
        assert hasattr(negotiation, name), name


def test_the_shared_model_stays_below_both_assessors() -> None:
    # The reason the model is its own module: the general assessment and
    # the faint recognition both need the vocabulary, and each needs the
    # other's verdicts. Only a layer underneath both keeps that acyclic,
    # so the model must import neither of them.
    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(assessment.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "scanmole.negotiation" not in imported
    assert "scanmole.faint" not in imported


def test_the_faint_path_reaches_the_model_directly() -> None:
    # The other half of the same rule: faint recognition takes the model
    # from where it lives, never back through the facade that imports it.
    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(faint.__file__).read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "scanmole.assessment" in imported
    assert "scanmole.negotiation" not in imported
