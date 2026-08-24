"""Tests for faint-mode negotiation, pinned against the real -A fixtures.

``lineart-auto`` is the one request that cannot be answered from a single
listing: recognizing a native enhancement takes staged set-and-reprobe
rounds, and failing to recognize one has to fall back to software. That
path lives in ``scanmole.faint``; the assessments every other request
shares stay in ``test_negotiation.py``.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scanmole.errors import DeviceError
from scanmole.negotiation import (
    Plan,
    Support,
    advisory_faint_assessment,
    assess_mode,
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


def _read_only(listing: str) -> dict[str, Capability]:
    return parse_capabilities(listing)


# ---- the fleet's faint requests, pinned ----------------------------------


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
    def failing(
        command: list[str], timeout_seconds: float, on_spawn: object = None
    ) -> None:
        raise subprocess.TimeoutExpired(command, timeout_seconds)

    monkeypatch.setattr("scanmole.options.run_command", failing)

    assert probe_snapshot("test:0") is None


# ---- read-only enhancement state is never something to emit --------------


def test_read_only_enhancement_counts_only_when_already_engaged() -> None:
    engaged = _read_only(
        "    --mode Lineart [Lineart]\n"
        "    --halftoning Text Enhanced Technology"
        " [Text Enhanced Technology] [read-only]\n"
    )
    enhancement = detect_native_enhancement(engaged)
    assert enhancement is not None
    assert enhancement.reason == "native-epson-tet"
    assert enhancement.settings == ()  # nothing may be emitted for it

    idle = _read_only(
        "    --mode Lineart [Lineart]\n"
        "    --halftoning Text Enhanced Technology [None] [read-only]\n"
    )
    assert detect_native_enhancement(idle) is None


def test_read_only_sdtc_counts_only_with_the_circuit_already_selected() -> None:
    engaged = _read_only(
        "    --threshold 0..255 [0] [read-only]\n"
        "    --variance 0..255 [0] [read-only]\n"
    )
    enhancement = detect_native_enhancement(engaged)
    assert enhancement is not None
    assert enhancement.reason == "native-fujitsu-sdtc"
    assert enhancement.settings == ()

    idle = _read_only(
        "    --threshold 0..255 [128] [read-only]\n"
        "    --variance 0..255 [0] [read-only]\n"
    )
    assert detect_native_enhancement(idle) is None


def _fixed_at(capability: Capability, current: str) -> Capability:
    """The same option as a ``[read-only]`` listing fixed at ``current``."""
    return replace(capability, settable=False, current=current)


def test_an_engaged_read_only_tet_resolves_native_without_emitting_a_mode() -> None:
    # The device is already in Lineart with the enhancement on and accepts
    # a value for neither. That is still a native faint scan; it just has
    # nothing to set, so the reprobe reads the state as it stands.
    caps = _fixture("epson-perfection1660-epson2.txt")
    caps["mode"] = _fixed_at(caps["mode"], "Lineart")
    caps["halftoning"] = _fixed_at(caps["halftoning"], "Text Enhanced Technology")
    prober = _Prober(caps)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober, (("--source", "ADF"),))

    assert plan.mode.support is Support.NATIVE
    assert plan.mode.reason == "native-epson-tet"
    assert plan.mode.effective == "lineart-auto"
    assert plan.mode.backend_value is None  # nothing to emit
    assert plan.extra_options == ()
    assert prober.calls == [(("--source", "ADF"),)]  # base settings only


def test_an_engaged_read_only_sdtc_resolves_native_without_emitting_a_mode() -> None:
    caps = _fixture("fujitsu-scansnap-ix500.txt")
    caps["mode"] = _fixed_at(caps["mode"], "Lineart")
    caps["threshold"] = _fixed_at(caps["threshold"], "0")
    prober = _Prober(caps)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober)

    assert plan.mode.support is Support.NATIVE
    assert plan.mode.reason == "native-fujitsu-sdtc"
    assert plan.mode.backend_value is None
    assert plan.extra_options == ()
    # No verification reprobe either: there is no setting to verify.
    assert prober.calls == [()]


def test_an_unengaged_read_only_enhancement_cannot_serve_the_request() -> None:
    # The same read-only topology with the enhancement parked on another
    # value: the device is conclusively fixed in plain 1-bit, which has
    # already discarded the shades the request is about. That is a failure
    # to deliver, not something to attempt and discover mid-batch.
    caps = _fixture("epson-perfection1660-epson2.txt")
    caps["mode"] = _fixed_at(caps["mode"], "Lineart")
    caps["halftoning"] = _fixed_at(caps["halftoning"], "Halftone A")

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.UNSUPPORTED
    assert plan.mode.reason == "no-information-preserving-path"
    assert plan.mode.backend_value is None
    assert plan.extra_options == ()
    assert advisory_faint_assessment(caps).support is Support.UNSUPPORTED

    sdtc = _fixture("fujitsu-scansnap-ix500.txt")
    sdtc["mode"] = _fixed_at(sdtc["mode"], "Lineart")
    sdtc["threshold"] = _fixed_at(sdtc["threshold"], "128")

    parked = resolve_faint_plan(_faint_plan(sdtc), sdtc, _Prober(sdtc))

    assert parked.mode.support is Support.UNSUPPORTED
    assert parked.extra_options == ()


def test_a_read_only_mode_on_a_non_lineart_value_is_not_a_candidate() -> None:
    # Read-only means the scan runs in whatever the device reports; a
    # device parked in Color cannot deliver a native 1-bit page, but it
    # can still serve the request through the software conversion.
    caps = _fixture("epson-perfection1660-epson2.txt")
    caps["mode"] = _fixed_at(caps["mode"], "Color")
    caps["halftoning"] = _fixed_at(caps["halftoning"], "Text Enhanced Technology")

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-color"
    assert plan.mode.actual == "Color"
    assert plan.mode.backend_value is None
    assert plan.extra_options == ()


def test_the_advisory_verdict_follows_an_engaged_read_only_enhancement() -> None:
    caps = _fixture("epson-perfection1660-epson2.txt")
    caps["mode"] = _fixed_at(caps["mode"], "Lineart")
    caps["halftoning"] = _fixed_at(caps["halftoning"], "Text Enhanced Technology")

    assessment = advisory_faint_assessment(caps)

    assert assessment.support is Support.NATIVE
    assert assessment.backend_value is None


@pytest.mark.parametrize(
    ("current", "reason"),
    [("Gray", "adaptive-gray"), ("Color", "adaptive-color")],
)
def test_a_read_only_gray_or_color_still_serves_the_faint_request(
    current: str, reason: str
) -> None:
    # A device parked in Gray or Color delivers exactly the brightness
    # data the guarded threshold needs. It cannot be set, which says
    # nothing about whether it can serve the request.
    caps = parse_capabilities(
        f"    --mode Lineart|Gray|Color [{current}] [read-only]\n"
        "    --depth 8|16 [8]\n"
        "    --resolution 300 [300]\n"
    )
    prober = _Prober(caps)

    plan = resolve_faint_plan(_faint_plan(caps), caps, prober)

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == reason
    assert plan.mode.backend_value is None  # never emitted
    assert plan.mode.actual == current  # but reported as the state it is
    assert plan.mode.effective == "lineart-auto"
    assert plan.extra_options == ()
    # The guarded threshold needs true 8-bit data, and the depth option
    # here is a separate, writable one.
    assert plan.depth.backend_value == "8"
    assert advisory_faint_assessment(caps).support is Support.EMULATED


def test_a_read_only_depth_is_not_pinned_for_the_adaptive_path() -> None:
    # Same request, but the depth cannot be set either. Nothing may be
    # emitted for it; the verdict is unchanged.
    caps = parse_capabilities(
        "    --mode Lineart|Gray [Gray] [read-only]\n"
        "    --depth 8|16 [8] [read-only]\n"
        "    --resolution 300 [300]\n"
    )

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.EMULATED
    assert plan.depth.backend_value is None


def test_a_read_only_mode_matching_nothing_known_is_unsupported() -> None:
    caps = parse_capabilities(
        "    --mode Lineart|Halftone [Halftone] [read-only]\n"
        "    --resolution 300 [300]\n"
    )

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.UNSUPPORTED
    assert plan.mode.reason == "no-matching-mode"
    assert "available: Halftone" in plan.mode.consequence


@pytest.mark.parametrize(
    "listing",
    [
        "    --mode Lineart|Gray [read-only]\n",  # read-only, no current value
        "    --mode Lineart|Gray [Gray] [inactive]\n",  # inactive: no evidence
    ],
)
def test_a_mode_without_usable_state_stays_unknown(listing: str) -> None:
    caps = parse_capabilities(f"{listing}    --resolution 300 [300]\n")

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.UNKNOWN
    assert plan.mode.actual is None
    assert advisory_faint_assessment(caps).support is Support.UNKNOWN


def test_the_writable_faint_fallback_is_unchanged() -> None:
    # The settable path emits the fallback mode and reports the same
    # string, exactly as before.
    caps = parse_capabilities(
        "    --mode Lineart|Gray|Color [Lineart]\n"
        "    --depth 8|16 [8]\n"
        "    --resolution 300 [300]\n"
    )

    plan = resolve_faint_plan(_faint_plan(caps), caps, _Prober(caps))

    assert plan.mode.support is Support.EMULATED
    assert plan.mode.reason == "adaptive-gray"
    assert plan.mode.backend_value == "Gray"
    assert plan.mode.actual == "Gray"
    assert plan.depth.backend_value == "8"

    color_only = parse_capabilities(
        "    --mode Lineart|Color [Lineart]\n    --resolution 300 [300]\n"
    )
    fallback = resolve_faint_plan(
        _faint_plan(color_only), color_only, _Prober(color_only)
    )
    assert fallback.mode.reason == "adaptive-color"
    assert fallback.mode.backend_value == "Color"


def test_the_gui_blocks_faint_on_a_device_fixed_in_plain_1_bit() -> None:
    # The GUI grays out UNSUPPORTED choices, so a device that can only
    # deliver plain 1-bit must not offer B/W (faint) at all.
    caps = parse_capabilities(
        "    --source ADF [ADF]\n"
        "    --mode Lineart|Gray [Lineart] [read-only]\n"
        "    --resolution 300 [300]\n"
    )

    modes = choice_support(caps).modes

    assert modes["lineart-auto"] is Support.UNSUPPORTED
    assert modes["lineart"] is Support.NATIVE  # ordinary B/W still works
    # Gray degrades to the 1-bit the device is on, as it always has: that
    # loses shades but still runs, which is not the same as being unable
    # to deliver the faint request at all.
    assert modes["gray"] is Support.DEGRADED

    # A device fixed in Gray keeps the choice selectable instead.
    gray = parse_capabilities(
        "    --mode Lineart|Gray [Gray] [read-only]\n    --resolution 300 [300]\n"
    )
    assert choice_support(gray).modes["lineart-auto"] is Support.EMULATED
