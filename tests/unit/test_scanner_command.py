"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.scanner import (
    _config,
)

from scanmole.errors import DeviceError
from scanmole.negotiation import negotiate, resolve_faint_plan
from scanmole.options import (
    Capability,
    is_feeder_source,
    is_flatbed_source,
    parse_capabilities,
)
from scanmole.scanner import (
    EffectiveSettings,
    build_scan_command,
)


def test_build_scan_command_uses_batch_print(tmp_path: Path) -> None:
    command, effective = build_scan_command(
        _config(), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-print" in command
    # An empty listing proves nothing, so the duplex request does not
    # survive into the pairing verdict: unrelated frames stay independent.
    assert effective == EffectiveSettings(
        source=None, mode=None, resolution=None, duplex=False
    )


def test_build_scan_command_auto_size_requests_the_full_window(
    tmp_path: Path,
) -> None:
    caps = {
        "x": Capability(kind="range", minimum=0.0, maximum=215.9),
        "y": Capability(kind="range", minimum=0.0, maximum=3098.8),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "215.9"
    assert command[command.index("-y") + 1] == "3098.8"


def test_build_scan_command_auto_size_clamps_the_area_to_the_page_limits(
    tmp_path: Path,
) -> None:
    # fujitsu backends advertise the -x/-y ranges of the *current* window
    # (A4 height) and only extend them once --page-width/--page-height are
    # raised, so the true limits are the page geometry maxima.
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("-x") + 1] == "221.121"
    assert command[command.index("-y") + 1] == "876.695"
    assert command.index("--page-height") < command.index("-y")


def test_build_scan_command_auto_size_enables_lower_edge_detection(
    tmp_path: Path,
) -> None:
    caps = {"ald": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    no_ald_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--ald=yes" in auto_command
    assert "--ald=yes" not in fixed_command
    assert "--ald=yes" not in no_ald_command


def test_build_scan_command_fixed_size_may_exceed_the_bare_axis_range(
    tmp_path: Path,
) -> None:
    caps = {
        "page-width": Capability(kind="range", minimum=0.0, maximum=221.121),
        "page-height": Capability(kind="range", minimum=0.0, maximum=876.695),
        "x": Capability(kind="range", minimum=0.0, maximum=215.872),
        "y": Capability(kind="range", minimum=0.0, maximum=279.364),
    }

    command, _effective = build_scan_command(
        _config(page_size="legal"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--page-height") + 1] == "355.6"
    assert command[command.index("-y") + 1] == "355.6"


def test_build_scan_command_auto_size_enables_hardware_adf_cropping(
    tmp_path: Path,
) -> None:
    # epsonds' "ADF auto cropping": white-backing devices cannot be cropped
    # in software, so auto page size hands the job to the hardware.
    caps = {"adf-crp": Capability(kind="bool"), "adf-skew": Capability(kind="bool")}

    auto_command, _ = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )
    fixed_command, _ = build_scan_command(
        _config(page_size="a4", deskew=True),
        "test:0",
        caps,
        str(tmp_path / "p_%04d.pnm"),
    )

    assert "--adf-crp=yes" in auto_command
    assert "--adf-crp=yes" not in fixed_command
    assert "--adf-skew=no" in auto_command
    assert "--adf-skew=yes" in fixed_command


def test_build_scan_command_degraded_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    # Flatbed-only device, duplex requested: the mapper degrades to the
    # flatbed, and --batch-count=1 must key on that mapped source or the
    # batch loops forever (a flatbed never reports "feeder empty").
    caps = {"source": Capability(kind="enum", choices=["Flatbed"])}

    command, effective = build_scan_command(
        _config(source="adf-duplex"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert effective.source == "Flatbed"
    assert command[command.index("--source") + 1] == "Flatbed"
    assert "--batch-count=1" in command


def test_build_scan_command_requested_flatbed_gets_batch_count(
    tmp_path: Path,
) -> None:
    command, _effective = build_scan_command(
        _config(source="flatbed"), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-count=1" in command


_FEEDER_SOURCES = Capability(
    kind="enum", choices=["ADF Front", "ADF Back", "ADF Duplex"]
)


def test_single_sheet_on_a_duplex_source_limits_to_two_frames(
    tmp_path: Path,
) -> None:
    # One physical sheet is two frames on a duplex source; --batch-count=1
    # would stop after the front side and split the sheet.
    caps = {"source": _FEEDER_SOURCES}

    command, effective = build_scan_command(
        _config(source="adf-duplex", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert effective.source == "ADF Duplex"
    assert "--batch-count=2" in command
    assert "--batch-count=1" not in command


@pytest.mark.parametrize("source", ["adf", "adf-back"])
def test_single_sheet_on_a_simplex_source_limits_to_one_frame(
    tmp_path: Path, source: str
) -> None:
    caps = {"source": _FEEDER_SOURCES}

    command, _effective = build_scan_command(
        _config(source=source, sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert command.count("--batch-count=1") == 1
    assert "--batch-count=2" not in command


def test_single_sheet_uses_the_degraded_effective_source(tmp_path: Path) -> None:
    # A simplex request on a duplex-only feeder runs duplex; the frame
    # limit must follow what the scanner will actually deliver, not the
    # request, or the sheet's back side is cut off.
    caps = {"source": Capability(kind="enum", choices=["ADF Duplex"])}

    command, effective = build_scan_command(
        _config(source="adf", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert effective.source == "ADF Duplex"
    assert "--batch-count=2" in command


def test_single_sheet_on_a_flatbed_keeps_one_batch_count(tmp_path: Path) -> None:
    caps = {"source": Capability(kind="enum", choices=["Flatbed"])}

    command, _effective = build_scan_command(
        _config(source="flatbed", sheet_flow="single"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert command.count("--batch-count=1") == 1


def test_single_sheet_refuses_unknown_source_evidence(tmp_path: Path) -> None:
    # Without a conclusively negotiated source ScanMole cannot know whether
    # one physical sheet is one frame or two; refuse before feeding paper.
    with pytest.raises(DeviceError, match="single"):
        build_scan_command(
            _config(source="adf-duplex", sheet_flow="single"),
            "test:0",
            {},
            str(tmp_path / "page_%04d.pnm"),
        )


def test_stack_flow_keeps_the_existing_command(tmp_path: Path) -> None:
    # The default flow must stay byte-for-byte what it was before sheet
    # flows existed: no frame limit on a feeder, --batch-count=1 on the
    # flatbed only.
    caps = {"source": _FEEDER_SOURCES}

    command, _effective = build_scan_command(
        _config(source="adf-duplex", sheet_flow="stack"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert not any(part.startswith("--batch-count") for part in command)
    assert not any(part.startswith("--batch-start") for part in command)


def test_batch_start_is_emitted_for_continuation_segments(tmp_path: Path) -> None:
    caps = {"source": _FEEDER_SOURCES}

    first, _ = build_scan_command(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )
    continuation, _ = build_scan_command(
        _config(source="adf", sheet_flow="collect"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
        batch_start=4,
    )

    assert not any(part.startswith("--batch-start") for part in first)
    assert "--batch-start=4" in continuation


def test_build_scan_command_auto_size_omits_geometry_without_ranges(
    tmp_path: Path,
) -> None:
    caps = {"x": Capability(kind="other")}

    command, _effective = build_scan_command(
        _config(page_size="auto"), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert "-x" not in command
    assert "-y" not in command


def test_build_scan_command_reports_the_snapped_resolution(tmp_path: Path) -> None:
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}

    command, effective = build_scan_command(
        _config(resolution=300), "test:0", caps, str(tmp_path / "page_%04d.pnm")
    )

    assert command[command.index("--resolution") + 1] == "150"
    assert effective.resolution == 150


def _read_only_caps() -> dict[str, Capability]:
    """A device whose whole option set is listed as read-only state."""
    return parse_capabilities(
        "    --source ADF Front [ADF Front] [read-only]\n"
        "    --mode Lineart [Lineart] [read-only]\n"
        "    --resolution 300 [300] [read-only]\n"
        "    -x 0..219.4mm [219.4] [read-only]\n"
        "    -y 0..297mm [297] [read-only]\n"
        "    --swdeskew[=(yes|no)] [no] [read-only]\n"
        "    --swcrop[=(yes|no)] [no] [read-only]\n"
        "    --swdespeck 0..9 [0] [read-only]\n"
        "    --ald[=(yes|no)] [no] [read-only]\n"
    )


def test_read_only_options_are_never_emitted(tmp_path: Path) -> None:
    # Writing a [read-only] option makes scanimage fail or ignore the
    # argument, so none of them may reach the command whatever they say.
    command, effective = build_scan_command(
        _config(source="adf", deskew=True, despeckle=1, page_size="auto"),
        "test:0",
        _read_only_caps(),
        str(tmp_path / "page_%04d.pnm"),
    )

    for option in (
        "--source",
        "--mode",
        "--resolution",
        "-x",
        "-y",
        "--ald=yes",
        "--swcrop=no",
        "--swcrop=yes",
        "--swdeskew=yes",
        "--swdeskew=no",
        "--swdespeck=1",
    ):
        assert option not in command, option
    # Read-only current values still establish state without emission:
    # the dpi, and the window the scan will actually run in.
    assert effective.resolution == 300
    assert effective.window_mm == (219.4, 297.0)
    assert effective.deskew_applied is False


def test_writable_geometry_and_controls_are_still_emitted(tmp_path: Path) -> None:
    caps = parse_capabilities(
        "    --source ADF Front [ADF Front]\n"
        "    --mode Lineart [Lineart]\n"
        "    -x 0..219.4mm [219.4]\n"
        "    -y 0..297mm [297]\n"
        "    --swdeskew[=(yes|no)] [no]\n"
        "    --swdespeck 0..9 [0]\n"
    )

    command, effective = build_scan_command(
        _config(source="adf", deskew=True, despeckle=1, page_size="a4"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "--source" in command and "--mode" in command
    assert "-x" in command and "-y" in command
    assert "--swdespeck=1" in command and "--swdeskew=yes" in command
    assert effective.deskew_applied is True


def test_unknown_source_evidence_never_claims_duplex(tmp_path: Path) -> None:
    # An UNKNOWN source echoes the request back, which is not proof. Both
    # the absent and the inactive listing must leave frames independent.
    for caps in (
        {"resolution": Capability(kind="range", minimum=50, maximum=600)},
        {"source": Capability(kind="enum", choices=["ADF Duplex"], active=False)},
    ):
        _command, effective = build_scan_command(
            _config(source="adf-duplex"),
            "test:0",
            caps,
            str(tmp_path / "page_%04d.pnm"),
        )
        assert effective.duplex is False


def test_conclusive_sources_keep_their_duplex_verdict(tmp_path: Path) -> None:
    _command, duplex_settings = build_scan_command(
        _config(source="adf-duplex"),
        "test:0",
        {"source": Capability(kind="enum", choices=["ADF Duplex"])},
        str(tmp_path / "page_%04d.pnm"),
    )
    assert duplex_settings.duplex is True

    # A duplex request degraded to a simplex feeder is conclusive and not
    # duplex; a flatbed likewise.
    for choice in ("ADF Front", "Flatbed"):
        _cmd, settings = build_scan_command(
            _config(source="adf-duplex"),
            "test:0",
            {"source": Capability(kind="enum", choices=[choice])},
            str(tmp_path / "page_%04d.pnm"),
        )
        assert settings.duplex is False, choice


def test_unknown_source_still_refuses_a_single_sheet_scan(tmp_path: Path) -> None:
    # The single-sheet flow keeps its refusal: without proof it cannot
    # promise one sheet is one frame or two.
    with pytest.raises(DeviceError, match="could not be negotiated"):
        build_scan_command(
            _config(source="adf-duplex", sheet_flow="single"),
            "test:0",
            {"resolution": Capability(kind="range", minimum=50, maximum=600)},
            str(tmp_path / "page_%04d.pnm"),
        )


def test_unknown_source_evidence_bounds_the_batch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A listing that proves nothing about the source may still belong to a
    # flatbed, which never reports "feeder empty", so an open-ended batch
    # would never stop. The request cannot settle it: it is the request
    # that is unverified. One frame per invocation, and say why.
    caps = {"resolution": Capability(kind="range", minimum=50, maximum=600)}

    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        command, _settings = build_scan_command(
            _config(source="adf-duplex", sheet_flow="stack"),
            "test:0",
            caps,
            str(tmp_path / "page_%04d.pnm"),
        )

    assert "--batch-count=1" in command
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "source capabilities" in warnings[0]
    assert "one frame" in warnings[0]

    # A flatbed request over the same listing is bounded for the same
    # reason, not because the request said flatbed.
    flatbed, _ = build_scan_command(
        _config(source="flatbed"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )
    assert "--batch-count=1" in flatbed


def test_conclusive_source_evidence_keeps_its_batch_behaviour(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Nothing changes where the listing settles the question: a proven
    # feeder still drains, a proven flatbed still stops after one frame,
    # and neither is worth warning about.
    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        feeder, _f = build_scan_command(
            _config(source="adf-duplex", sheet_flow="stack"),
            "test:0",
            {"source": _FEEDER_SOURCES},
            str(tmp_path / "page_%04d.pnm"),
        )
        flatbed, _b = build_scan_command(
            _config(source="flatbed"),
            "test:0",
            {"source": Capability(kind="enum", choices=["Flatbed"])},
            str(tmp_path / "page_%04d.pnm"),
        )

    assert not any(part.startswith("--batch-count") for part in feeder)
    assert "--batch-count=1" in flatbed
    assert caplog.records == []


def test_the_unknown_source_warning_is_not_repeated_per_segment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Collect rebuilds the command for every continuation segment. Each
    # one stays bounded; only the run's first command explains it.
    caps = {"resolution": Capability(kind="range", minimum=50, maximum=600)}

    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        first, _a = build_scan_command(
            _config(source="adf", sheet_flow="collect"),
            "test:0",
            caps,
            str(tmp_path / "page_%04d.pnm"),
        )
        for start in (3, 5):
            segment, _b = build_scan_command(
                _config(source="adf", sheet_flow="collect"),
                "test:0",
                caps,
                str(tmp_path / "page_%04d.pnm"),
                None,
                start,
            )
            assert "--batch-count=1" in segment
            assert f"--batch-start={start}" in segment

    assert "--batch-count=1" in first
    assert len(caplog.records) == 1


def test_unknown_source_evidence_still_refuses_a_single_sheet_flow(
    tmp_path: Path,
) -> None:
    # Bounding the batch does not make one frame one sheet: a duplex
    # source needs two, and that is exactly what is unproven here.
    with pytest.raises(DeviceError, match="could not be negotiated"):
        build_scan_command(
            _config(source="adf-duplex", sheet_flow="single"),
            "test:0",
            {"resolution": Capability(kind="range", minimum=50, maximum=600)},
            str(tmp_path / "page_%04d.pnm"),
        )


def _window_caps(x: str, y: str) -> dict[str, Capability]:
    """A read-only listing whose only evidence is its x/y current values."""
    return parse_capabilities(f"    --resolution 300 [300] [read-only]\n{x}{y}")


def test_read_only_window_values_become_the_effective_window(
    tmp_path: Path,
) -> None:
    # A [read-only] -x/-y cannot be set, but its current value is the
    # width and height the backend will use, which is exactly what
    # automatic page size measures each frame against.
    command, effective = build_scan_command(
        _config(page_size="auto"),
        "test:0",
        _window_caps(
            "    -x 0..215.9mm [215.9] [read-only]\n",
            "    -y 0..297.18mm [297.18] [read-only]\n",
        ),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "-x" not in command and "-y" not in command
    assert effective.window_mm == (215.9, 297.18)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        # A range maximum is a limit, not the window in force.
        ("    -x 0..215.9mm [read-only]\n", "    -y 0..297.18mm [read-only]\n"),
        # Opaque or malformed markers are not measurements.
        (
            "    -x 0..215.9mm [<float>] [read-only]\n",
            "    -y 0..297.18mm [auto] [read-only]\n",
        ),
        # Neither is a value that cannot be a physical extent.
        (
            "    -x 0..215.9mm [0] [read-only]\n",
            "    -y 0..297.18mm [-5] [read-only]\n",
        ),
        (
            "    -x 0..215.9mm [inf] [read-only]\n",
            "    -y 0..297.18mm [nan] [read-only]\n",
        ),
        (
            "    -x 0..215.9mm [" + "9" * 400 + "] [read-only]\n",
            "    -y 0..297.18mm [297.18] [read-only]\n",
        ),
        # One axis alone describes no window.
        ("    -x 0..215.9mm [215.9] [read-only]\n", ""),
        ("", "    -y 0..297.18mm [297.18] [read-only]\n"),
    ],
)
def test_unusable_read_only_window_values_carry_no_window(
    tmp_path: Path, x: str, y: str
) -> None:
    _command, effective = build_scan_command(
        _config(page_size="auto"),
        "test:0",
        _window_caps(x, y),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert effective.window_mm is None


def test_writable_geometry_keeps_reporting_what_it_requested(tmp_path: Path) -> None:
    # The read-only fallback must not touch an axis the command set
    # itself: there the emitted value is the window, current value or not.
    command, effective = build_scan_command(
        _config(page_size="a4"),
        "test:0",
        parse_capabilities(
            "    --resolution 300 [300]\n"
            "    -x 0..215.9mm [215.9]\n"
            "    -y 0..297.18mm [297.18]\n"
        ),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "-x" in command and "-y" in command
    assert effective.window_mm == (210.0, 297.0)  # the A4 request, not the maxima


def test_a_read_only_source_is_state_without_being_emitted(tmp_path: Path) -> None:
    # The device is fixed to ADF Front. Nothing may be written for it, but
    # the scan still runs from that source, and the pipeline sizes pages
    # by it, so the settings must say so.
    command, effective = build_scan_command(
        _config(source="adf-duplex", mode="lineart"),
        "test:0",
        parse_capabilities(
            "    --source ADF Front [ADF Front] [read-only]\n"
            "    --mode Lineart|Gray [Lineart] [read-only]\n"
            "    --resolution 300 [300]\n"
        ),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "--source" not in command and "--mode" not in command
    assert effective.source == "ADF Front"
    assert effective.mode == "Lineart"
    # Degraded from the duplex request, and pairing still needs the
    # abstract verdict rather than the backend string.
    assert effective.duplex is False


def test_a_read_only_flatbed_is_recognized_as_a_flatbed(tmp_path: Path) -> None:
    # A duplex request on a device fixed to the flatbed: the request says
    # feeder, the device says otherwise, and the device is right. It also
    # never reports "feeder empty", so the frame limit applies.
    command, effective = build_scan_command(
        _config(source="adf-duplex", mode="gray"),
        "test:0",
        parse_capabilities(
            "    --source Flatbed [Flatbed] [read-only]\n    --resolution 300 [300]\n"
        ),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "--source" not in command
    assert "--batch-count=1" in command
    assert effective.source == "Flatbed"
    assert is_flatbed_source(effective.source)
    assert is_feeder_source(effective.source) is False


def test_unknown_source_and_mode_never_borrow_the_request(tmp_path: Path) -> None:
    # The listing establishes nothing. The requested values describe what
    # was asked for, not what the device does, so they must not travel as
    # if they had been confirmed.
    command, effective = build_scan_command(
        _config(source="adf-duplex", mode="color"),
        "test:0",
        {"resolution": Capability(kind="range", minimum=50, maximum=600)},
        str(tmp_path / "page_%04d.pnm"),
    )

    assert "--source" not in command and "--mode" not in command
    assert effective.source is None
    assert effective.mode is None
    assert effective.duplex is False
    assert "--batch-count=1" in command  # the bounded batch still applies


def test_writable_source_and_mode_are_emitted_and_reported(tmp_path: Path) -> None:
    # Unchanged where the option is settable: the emitted value and the
    # state are the same string.
    command, effective = build_scan_command(
        _config(source="adf-duplex", mode="gray"),
        "test:0",
        parse_capabilities(
            "    --source ADF Duplex|ADF Front|Flatbed [ADF Front]\n"
            "    --mode Lineart|Gray|Color [Lineart]\n"
            "    --resolution 300 [300]\n"
        ),
        str(tmp_path / "page_%04d.pnm"),
    )

    assert command[command.index("--source") + 1] == "ADF Duplex"
    assert command[command.index("--mode") + 1] == "Gray"
    assert effective.source == "ADF Duplex"
    assert effective.mode == "Gray"
    assert effective.duplex is True


@pytest.mark.parametrize(
    ("enhancement", "expected"),
    [
        (
            "    --halftoning Text Enhanced Technology|Halftone A [Halftone A]\n",
            [("--halftoning", "Text Enhanced Technology")],
        ),
        (
            "    --threshold 0..255 [128]\n    --variance 0..255 [0]\n",
            [("--threshold", "0"), ("--variance", "0")],
        ),
    ],
)
def test_a_read_only_mode_still_emits_a_writable_enhancement(
    tmp_path: Path, enhancement: str, expected: list[tuple[str, str]]
) -> None:
    # The device is fixed in Lineart but its enhancement can be set. Only
    # the writable half reaches argv; --mode never does.
    caps = parse_capabilities(
        "    --source ADF [ADF]\n"
        "    --mode Lineart|Gray [Lineart] [read-only]\n"
        f"{enhancement}"
        "    --resolution 300 [300]\n"
    )
    config = _config(source="adf", mode="lineart", lineart_threshold="auto")
    plan = negotiate(
        caps,
        source=config.source,
        mode=config.mode,
        resolution=config.resolution,
        lineart_threshold=config.lineart_threshold,
    )
    plan = resolve_faint_plan(plan, caps, lambda _settings: caps)

    command, effective = build_scan_command(
        config, "test:0", caps, str(tmp_path / "page_%04d.pnm"), plan
    )

    assert "--mode" not in command
    for option, value in expected:
        assert command[command.index(option) + 1] == value
    assert effective.mode == "Lineart"
    assert effective.faint_native is True


# Which mechanism straightens a page is settled here and travels as one
# boolean. Downstream code reads EffectiveSettings.deskew_applied and
# never the option names, so moving the policy later stays a change to
# negotiation and command construction alone.


@pytest.mark.parametrize("option", ["swdeskew", "adf-skew"])
@pytest.mark.parametrize("requested", [True, False])
def test_a_backend_deskew_option_is_set_and_reported(
    tmp_path: Path, option: str, requested: bool
) -> None:
    caps = parse_capabilities(
        "    --source ADF Front [ADF Front]\n"
        "    --mode Lineart [Lineart]\n"
        f"    --{option}[=(yes|no)] [no]\n"
    )

    command, effective = build_scan_command(
        _config(source="adf", deskew=requested, page_size="a4"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    # The option is always emitted, so a device default cannot straighten
    # a page the run asked to keep.
    assert f"--{option}={'yes' if requested else 'no'}" in command
    assert effective.deskew_applied is requested


@pytest.mark.parametrize("requested", [True, False])
def test_both_deskew_options_together_report_one_verdict(
    tmp_path: Path, requested: bool
) -> None:
    # A device offering both gets both set; the verdict stays a single
    # boolean rather than something downstream has to combine.
    caps = parse_capabilities(
        "    --source ADF Front [ADF Front]\n"
        "    --mode Lineart [Lineart]\n"
        "    --swdeskew[=(yes|no)] [no]\n"
        "    --adf-skew[=(yes|no)] [no]\n"
    )

    command, effective = build_scan_command(
        _config(source="adf", deskew=requested, page_size="a4"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    answer = "yes" if requested else "no"
    assert f"--swdeskew={answer}" in command
    assert f"--adf-skew={answer}" in command
    assert effective.deskew_applied is requested


def test_a_read_only_deskew_option_leaves_the_request_unclaimed(
    tmp_path: Path,
) -> None:
    # Listed but not settable is not a mechanism: the backend cannot be
    # told to straighten, so the verdict must not claim it did.
    caps = parse_capabilities(
        "    --source ADF Front [ADF Front]\n"
        "    --mode Lineart [Lineart]\n"
        "    --swdeskew[=(yes|no)] [no] [read-only]\n"
    )

    command, effective = build_scan_command(
        _config(source="adf", deskew=True, page_size="a4"),
        "test:0",
        caps,
        str(tmp_path / "page_%04d.pnm"),
    )

    assert not any(argument.startswith("--swdeskew") for argument in command)
    assert effective.deskew_applied is False
