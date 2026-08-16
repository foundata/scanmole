"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scanmole.config import ScanConfig
from scanmole.errors import DeviceError, NoPagesError, ProcessingError, ScanMoleError
from scanmole.events import EventWriter
from scanmole.options import Capability
from scanmole.scanner import (
    EffectiveSettings,
    build_scan_command,
    run_scanimage,
    scan_to_files,
)


def _config(**overrides: object) -> ScanConfig:
    values: dict[str, object] = {
        "device": None,
        "source": "adf-duplex",
        "mode": "lineart",
        "resolution": 300,
        "page_size": "a4",
        "despeckle": 1,
        "deskew": False,
        "crop": False,
        "ocr": False,
        "lang": "deu",
        "rotate_pages": True,
        "optimize": 1,
        "pdfa": False,
        "blank_threshold": 0.995,
        "keep_blanks": False,
        "from_images": None,
        "keep_images": None,
        "output": Path("out.pdf"),
    }
    values.update(overrides)
    return ScanConfig(**values)  # type: ignore[arg-type]


def test_run_scanimage_reports_pages_printed_on_stdout(tmp_path: Path) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    exit_code, stderr = run_scanimage(
        [
            "sh",
            "-c",
            f"echo '{first}'; echo 'Scanned page 1.' >&2; echo '{second}'; exit 7",
        ],
        seen.append,
    )

    assert exit_code == 7
    assert seen == [first, second]
    assert "Scanned page 1." in stderr


def test_run_scanimage_ignores_non_page_stdout_lines(tmp_path: Path) -> None:
    seen: list[Path] = []

    run_scanimage(["sh", "-c", "echo 'not a page'; echo ''"], seen.append)

    assert seen == []


def test_run_scanimage_propagates_page_callback_failures(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"

    def failing_callback(path: Path) -> None:
        raise RuntimeError("callback exploded")

    # The sleep would stall the test for 30s if the failing callback did not
    # terminate the subprocess promptly.
    with pytest.raises(ScanMoleError, match="callback exploded") as info:
        run_scanimage(["sh", "-c", f"echo '{page}'; sleep 30"], failing_callback)

    assert isinstance(info.value.__cause__, RuntimeError)


def test_run_scanimage_propagates_domain_errors_unwrapped(tmp_path: Path) -> None:
    page = tmp_path / "page_0001.pnm"
    error = ProcessingError("event stream gone")

    def failing_callback(path: Path) -> None:
        raise error

    with pytest.raises(ProcessingError) as info:
        run_scanimage(["sh", "-c", f"echo '{page}'"], failing_callback)

    assert info.value is error


def test_run_scanimage_stops_delivering_after_a_callback_failure(
    tmp_path: Path,
) -> None:
    first = tmp_path / "page_0001.pnm"
    second = tmp_path / "page_0002.pnm"
    seen: list[Path] = []

    def failing_callback(path: Path) -> None:
        seen.append(path)
        raise RuntimeError("boom")

    with pytest.raises(ScanMoleError):
        run_scanimage(
            ["sh", "-c", f"echo '{first}'; echo '{second}'"], failing_callback
        )

    assert seen == [first]


def test_build_scan_command_uses_batch_print(tmp_path: Path) -> None:
    command, effective = build_scan_command(
        _config(), "test:0", {}, str(tmp_path / "page_%04d.pnm")
    )

    assert "--batch-print" in command
    assert effective == EffectiveSettings(source=None, mode=None, resolution=None)


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


def test_scan_to_files_returns_the_effective_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, source=None: caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p: None,
    )

    assert result.settings.resolution == 150


def test_scan_to_files_reprobes_with_the_mapped_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {"source": Capability(kind="enum", choices=["ADF", "ADF Duplex"])}
    probes: list[str | None] = []

    def fake_probe(device: str, source: str | None = None) -> dict[str, Capability]:
        probes.append(source)
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
        lambda p: None,
    )

    assert probes == [None, "ADF Duplex"]


def test_scan_to_files_sweeps_pages_scanimage_did_not_announce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("page_0002.pnm", "page_0001.pnm"):
        (tmp_path / name).write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, source=None: {}
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(), "test:0", tmp_path, EventWriter(enabled=False), seen.append
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert seen == result.pages


def test_scan_to_files_raises_when_nothing_was_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, source=None: {}
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(), "test:0", tmp_path, EventWriter(enabled=False), lambda p: None
        )


def test_scan_to_files_reports_scan_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, source=None: {}
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage",
        lambda command, on_page: (1, "scanimage: sane_start failed"),
    )

    with pytest.raises(DeviceError, match="sane_start failed"):
        scan_to_files(
            _config(), "test:0", tmp_path, EventWriter(enabled=False), lambda p: None
        )
