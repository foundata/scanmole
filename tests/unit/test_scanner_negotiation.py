"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.scanner import (
    _config,
)

from scanmole.errors import DeviceError
from scanmole.events import EventWriter
from scanmole.options import (
    Capability,
    parse_capabilities,
)
from scanmole.scanner import (
    scan_to_files,
)


def test_scan_to_files_reprobes_with_the_mapped_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF", "ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    probes: list[tuple[tuple[str, str], ...]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    # Bare, source-applied, the acquisition state the resolution is
    # assessed against (no mode option exists here), and finally the same
    # state with that resolution applied, which is what the geometry and
    # the sensor reads are taken from.
    assert probes == [
        (),
        (("--source", "ADF Duplex"),),
        (("--source", "ADF Duplex"),),
        (("--source", "ADF Duplex"), ("--resolution", "300")),
    ]


def _shrinking_window_probe(
    fail_with_resolution: bool = False, window: bool = True
) -> Callable[..., dict[str, Capability]]:
    """A backend whose window shrinks once the resolution is applied.

    Advertises 216x900 mm until the dpi is set and 200x300 mm at 600 dpi,
    which is the constraint reload SANE explicitly permits. With
    ``fail_with_resolution`` the resolution-applied listing is refused, so
    only the stale pre-resolution geometry would be left.
    """

    def probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        applied = dict(settings)
        with_resolution = applied.get("--resolution") == "600"
        if with_resolution and fail_with_resolution:
            raise DeviceError("cannot list with a resolution applied")
        caps: dict[str, Capability] = {
            "resolution": Capability(kind="enum", choices=["300", "600"])
        }
        if window:
            width, height = (200.0, 300.0) if with_resolution else (216.0, 900.0)
            caps["x"] = Capability(kind="range", minimum=0, maximum=width)
            caps["y"] = Capability(kind="range", minimum=0, maximum=height)
        return caps

    return probe


def test_geometry_is_read_with_the_negotiated_resolution_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SANE lets any option change reload every other constraint, and a
    # window that shrinks at the chosen dpi is exactly that: both axes
    # must come from the snapshot the scan's own state produces, not from
    # one taken before the resolution was applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", _shrinking_window_probe()
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    assert command[command.index("-x") + 1] == "200"
    assert command[command.index("-y") + 1] == "300"
    # The window the pipeline arms content sizing with must be the one the
    # scan actually ran in; 216x900 would make a full frame look cropped.
    assert result.settings.window_mm == (200.0, 300.0)


def test_auto_size_refuses_a_window_the_resolution_never_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Automatic page size decides whether a frame was hardware-cropped by
    # comparing it against the requested window, so a stale window is not
    # a harmless fallback: a 216x300 frame from a window recorded as
    # 216x900 reads as cropped and skips content sizing entirely. Refuse
    # before any paper moves rather than mis-size the result.
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True),
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("no paper may be fed on unverified geometry")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="scan window"):
        scan_to_files(
            _config(resolution=600, page_size="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_a_fixed_page_size_survives_a_refused_resolution_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fixed size never compares the frame against the window, so the
    # stale listing costs nothing and the tolerant path stands.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True),
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="a4"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands and result.settings.resolution == 600


def test_a_refused_resolution_listing_without_a_window_still_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No x/y evidence means no window travels either way, so the optional
    # probe failing must not invent a new reason to refuse.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        _shrinking_window_probe(fail_with_resolution=True, window=False),
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=600, page_size="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands and result.settings.window_mm is None


def _fixture_caps(name: str) -> dict[str, Capability]:
    from scanmole.options import parse_capabilities

    fixtures = Path(__file__).parent.parent / "fixtures" / "scanimage-A"
    return parse_capabilities((fixtures / name).read_text())


def test_scan_refuses_without_resolution_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --resolution option at all: the effective dpi stays UNKNOWN and
    # acquisition must fail before any paper is fed.
    caps = {"source": Capability(kind="enum", choices=["ADF Duplex"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="physical resolution"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_fixed_resolution_reaches_settings_without_being_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inactive option pinned to 200 dpi establishes the effective dpi:
    # nothing is emitted, but the settings and the settings event carry it.
    import io
    import json

    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="enum", choices=["200dpi"], active=False),
    }
    commands: list[list[str]] = []

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert "--resolution" not in commands[0]
    assert result.settings.resolution == 200
    settings_event = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "settings"
    )
    assert settings_event["resolution"] == 200


def test_source_dependent_snapshot_decides_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The authoritative source-applied listing advertises a smaller range
    # than the bare one; the emitted and reported dpi must follow it.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    bare = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    sourced = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "resolution": Capability(kind="range", minimum=50, maximum=150),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = ()
    ) -> dict[str, Capability]:
        return sourced if settings else bare

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def _res(minimum: float, maximum: float) -> Capability:
    return Capability(kind="range", minimum=minimum, maximum=maximum)


def test_mode_dependent_resolution_is_renegotiated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 50..600 dpi in the bare and source listings, but only 50..150 once
    # Lineart is applied: the emitted and reported dpi must come from the
    # final acquisition state, not the earlier optimistic range.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        applied_mode = any(option == "--mode" for option, _value in settings)
        return {**base, "resolution": _res(50, 150 if applied_mode else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_software_faint_resolution_follows_the_gray_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The adaptive faint path scans Gray: the dpi must come from the probe
    # with Gray (and the pinned depth) applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    base = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart", "Gray"]),
    }
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        gray = ("--mode", "Gray") in settings
        return {**base, "resolution": _res(50, 150 if gray else 600)}

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_native_faint_resolution_follows_the_enhanced_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native SDTC path applies Lineart plus its extras; the dpi must
    # come from the probe with that complete state applied.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
        if ("--mode", "Lineart") in settings:
            caps["resolution"] = _res(50, 150)
        return caps

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(resolution=300, lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--threshold") + 1] == "0"  # native path
    assert commands[0][commands[0].index("--resolution") + 1] == "150"
    assert result.settings.resolution == 150


def test_faint_command_engages_fujitsu_sdtc_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    probes: list[tuple[tuple[str, str], ...]] = []
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        probes.append(tuple(settings))
        return _fixture_caps("fujitsu-scansnap-ix500.txt")

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)
    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    source = ("--source", "ADF Duplex")
    final = (source, ("--mode", "Lineart"), ("--threshold", "0"), ("--variance", "0"))
    assert probes == [
        (),
        (source,),
        (source, ("--mode", "Lineart")),
        final,  # the SDTC verification reprobe
        final,  # the final-state probe deciding the resolution
        (*final, ("--resolution", "300")),  # geometry, with that dpi applied
    ]
    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 6] == [
        "--mode",
        "Lineart",
        "--threshold",
        "0",
        "--variance",
        "0",
    ]
    assert "--depth" not in command
    assert result.settings.faint_native is True


def test_faint_command_engages_epson_tet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps(
            "epson-perfection1660-epson2.txt"
        ),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    scan_to_files(
        _config(lineart_threshold="auto", source="flatbed", page_size="a4"),
        "epson2:libusb:001:004",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    mode_at = command.index("--mode")
    assert command[mode_at : mode_at + 4] == [
        "--mode",
        "Lineart",
        "--halftoning",
        "Text Enhanced Technology",
    ]


def test_faint_fallback_scans_gray_with_pinned_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The epsonds DS-730N has no native enhancement: the faint scan must
    # acquire Gray at an explicit 8 bit, never the device's plain Lineart.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: _fixture_caps("epson-ds730n-epsonds.txt"),
    )

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "epsonds:net:192.168.0.167",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    command = commands[0]
    assert command[command.index("--mode") + 1] == "Gray"
    assert command[command.index("--depth") + 1] == "8"
    assert result.settings.faint_native is False


def test_faint_on_a_lineart_only_device_fails_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "mode": Capability(kind="enum", choices=["Lineart"]),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(), **_kw: caps,
    )

    def never_run(command: list[str], on_page: object) -> tuple[int, str]:
        raise AssertionError("acquisition must not start")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never_run)

    with pytest.raises(DeviceError, match="ordinary B/W"):
        scan_to_files(
            _config(lineart_threshold="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_faint_candidate_probe_failure_falls_back_to_gray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bare and source-applied probes succeed; the candidate-mode probe
    # raises. The scan must fall back to the software path, not abort.
    (tmp_path / "page_0001.pnm").write_bytes(b"P5\n1 1\n255\n\x80")
    caps = _fixture_caps("fujitsu-scansnap-ix500.txt")
    commands: list[list[str]] = []

    def fake_probe(
        device: str, settings: tuple[tuple[str, str], ...] = (), **_kw: object
    ) -> dict[str, Capability]:
        if ("--mode", "Lineart") in settings:  # only the candidate probes
            raise DeviceError("timed out probing options")
        return caps

    monkeypatch.setattr("scanmole.scanner.probe_capabilities", fake_probe)

    def fake_run(command: list[str], on_page: object) -> tuple[int, str]:
        commands.append(command)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    result = scan_to_files(
        _config(lineart_threshold="auto"),
        "fujitsu:iX500",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert commands[0][commands[0].index("--mode") + 1] == "Gray"
    assert result.settings.faint_native is False


def test_the_settings_event_reports_read_only_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end through the engine: what the frontend is told about the
    # scan is the device's actual state, not the subset that was emitted.
    caps = parse_capabilities(
        "    --source ADF Front [ADF Front] [read-only]\n"
        "    --mode Lineart|Gray [Lineart] [read-only]\n"
        "    --resolution 300 [300] [read-only]\n"
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    issued: list[list[str]] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        issued.append(cmd)
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    scan_to_files(
        _config(source="adf-duplex", mode="lineart"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert "--source" not in issued[0] and "--mode" not in issued[0]
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    settings = next(event for event in events if event["event"] == "settings")
    # The frozen key set, unchanged.
    assert set(settings) == {"event", "device", "source", "mode", "resolution"}
    assert settings["source"] == "ADF Front"
    assert settings["mode"] == "Lineart"
    assert settings["resolution"] == 300


def test_the_settings_event_stays_null_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The counterpart: nothing established, so nothing claimed. The
    # requested source and mode must not appear here.
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    scan_to_files(
        _config(source="adf-duplex", mode="color"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    settings = next(event for event in events if event["event"] == "settings")
    assert set(settings) == {"event", "device", "source", "mode", "resolution"}
    assert settings["source"] is None
    assert settings["mode"] is None


def test_a_read_only_gray_faint_scan_emits_no_mode_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A device fixed in Gray serves the faint request through the software
    # conversion. Nothing may be emitted for the mode, the depth the
    # guarded threshold needs is emitted because that option is writable,
    # and the frontend is told the mode the scan actually runs in.
    caps = parse_capabilities(
        "    --source ADF [ADF]\n"
        "    --mode Lineart|Gray|Color [Gray] [read-only]\n"
        "    --depth 8|16 [8]\n"
        "    --resolution 300 [300]\n"
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    issued: list[list[str]] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        issued.append(cmd)
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P5\n1 1\n255\n\xc8")
        on_page(page)
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    stream = io.StringIO()

    scan_to_files(
        _config(source="adf", mode="lineart", lineart_threshold="auto"),
        "test:0",
        tmp_path,
        EventWriter(enabled=True, stream=stream),
        lambda p, o: None,
    )

    assert "--mode" not in issued[0]
    assert issued[0][issued[0].index("--depth") + 1] == "8"
    settings = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"] == "settings"
    )
    assert set(settings) == {"event", "device", "source", "mode", "resolution"}
    assert settings["mode"] == "Gray"


def test_a_read_only_plain_lineart_faint_scan_refuses_before_acquiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The device is conclusively fixed in plain 1-bit, which cannot carry
    # faint shades. Refusing has to happen before any paper moves, not
    # after the first frame arrives.
    caps = parse_capabilities(
        "    --source ADF [ADF]\n"
        "    --mode Lineart|Gray [Lineart] [read-only]\n"
        "    --resolution 300 [300]\n"
    )
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    runs: list[list[str]] = []

    def fake_run(cmd: list[str], on_page: Callable[[Path], None]) -> tuple[int, str]:
        runs.append(cmd)  # pragma: no cover -- must never be reached
        return 0, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)

    with pytest.raises(DeviceError, match="only plain 1-bit"):
        scan_to_files(
            _config(source="adf", mode="lineart", lineart_threshold="auto"),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )

    assert runs == []
    assert list(tmp_path.iterdir()) == []
