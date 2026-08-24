"""Tests for batch acquisition, without scanner hardware.

``run_scanimage`` is exercised with a shell stand-in for scanimage;
``scan_to_files`` with monkeypatched probing and scanning.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support.scanner import (
    _config,
)

from scanmole.errors import DeviceError, NoPagesError
from scanmole.events import EventWriter
from scanmole.options import (
    Capability,
)
from scanmole.scanner import (
    build_scan_command,
    scan_to_files,
)
from scanmole.sheetflow import PageOrigin


def test_scan_to_files_returns_the_effective_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {"resolution": Capability(kind="enum", choices=["150", "600"])}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    result = scan_to_files(
        _config(resolution=300),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: None,
    )

    assert result.settings.resolution == 150


def test_scan_to_files_sweeps_pages_scanimage_did_not_announce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("page_0002.pnm", "page_0001.pnm"):
        (tmp_path / name).write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append(p),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm", "page_0002.pnm"]
    assert seen == result.pages


def test_a_swept_page_reports_the_lost_announcement_not_a_lost_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # page_NNNN.pnm only exists once scanimage renamed it from .part, so a
    # swept file is a completed page and is delivered as one. What went
    # missing is the announcement, and that is what the warning says: it
    # must not cast doubt on the page or send the user to a result that
    # processing may still fail to produce.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with caplog.at_level("WARNING", logger="scanmole.scanner"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )

    warnings = [record.getMessage() for record in caplog.records]
    assert any(
        "page_0001.pnm" in text and "did not announce" in text for text in warnings
    )
    assert not any("incomplete" in text for text in warnings)


def test_scan_to_files_delivers_segment_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Announced and swept frames alike carry their acquisition identity;
    # the frame index comes from the file number, so an unannounced final
    # frame still lands on the physical sheet it belongs to.
    (tmp_path / "page_0002.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )

    def fake_run(
        command: list[str], on_page: Callable[[Path], None]
    ) -> tuple[int, str]:
        page = tmp_path / "page_0001.pnm"
        page.write_bytes(b"P4\n1 1\n\x00")
        on_page(page)
        return 7, ""

    monkeypatch.setattr("scanmole.scanner.run_scanimage", fake_run)
    origins: list[tuple[str, PageOrigin]] = []

    scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: origins.append((p.name, o)),
    )

    assert origins == [
        ("page_0001.pnm", PageOrigin(segment=1, frame=1)),
        ("page_0002.pnm", PageOrigin(segment=1, frame=2)),
    ]


def test_scan_to_files_raises_when_nothing_was_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with pytest.raises(NoPagesError):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_scan_to_files_reports_scan_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage",
        lambda command, on_page: (1, "scanimage: sane_start failed"),
    )

    with pytest.raises(DeviceError, match="sane_start failed"):
        scan_to_files(
            _config(),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_only_a_chosen_backend_marks_the_request_as_applied() -> None:
    # The one boolean the pipeline reads. It follows the settled owner,
    # not the presence of an option: a device that offers a mechanism
    # nobody selected has not taken the request.
    caps = {
        "source": Capability(kind="enum", choices=["ADF Duplex"]),
        "swdeskew": Capability(kind="bool"),
    }

    _, automatic = build_scan_command(
        _config(deskew=True), "dev", caps, "out/page_%04d.pnm"
    )
    _, forced = build_scan_command(
        _config(deskew=True, deskew_method="scanner"), "dev", caps, "out/page_%04d.pnm"
    )
    _, without = build_scan_command(
        _config(deskew=False), "dev", caps, "out/page_%04d.pnm"
    )
    _, no_option = build_scan_command(
        _config(deskew=True),
        "dev",
        {"source": Capability(kind="enum", choices=["ADF Duplex"])},
        "out/page_%04d.pnm",
    )

    assert automatic.deskew_applied is False  # ScanMole keeps it by default
    assert forced.deskew_applied is True
    assert without.deskew_applied is False  # the option was set to =no
    assert no_option.deskew_applied is False  # nothing there to take the job


@pytest.mark.parametrize(
    ("method", "requested", "listing"),
    [
        pytest.param("scanner", True, {}, id="scanner-without-a-mechanism"),
        pytest.param(
            "scanmole",
            True,
            {"swdeskew": Capability(kind="bool", settable=False, current="yes")},
            id="scanmole-against-an-unstoppable-one",
        ),
        pytest.param(
            "auto",
            False,
            {"swdeskew": Capability(kind="bool", settable=False, current="yes")},
            id="no-deskew-against-an-unstoppable-one",
        ),
    ],
)
def test_an_impossible_deskew_owner_refuses_before_the_feeder_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    requested: bool,
    listing: dict[str, Capability],
) -> None:
    # The refusal is worth nothing after the paper has gone through, so
    # it has to happen while the stack is still in the tray: scanimage
    # must never be reached.
    caps = {"resolution": Capability(kind="enum", choices=["300"]), **listing}
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )

    def never(command: object, on_page: object) -> tuple[int, str]:
        raise AssertionError("the scan started despite an impossible deskew owner")

    monkeypatch.setattr("scanmole.scanner.run_scanimage", never)

    with pytest.raises(DeviceError):
        scan_to_files(
            _config(deskew=requested, deskew_method=method),
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )


def test_scan_to_files_warns_exactly_once_per_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Negotiation runs twice (initial probe, source-applied reprobe) but the
    # selected plan's notices must reach the user exactly once.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")
    caps = {
        "source": Capability(kind="enum", choices=["ADF Front"]),
        "resolution": Capability(kind="range", minimum=50, maximum=600),
    }
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities", lambda device, settings=(): caps
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )

    with caplog.at_level("INFO"):
        scan_to_files(
            _config(),  # requests adf-duplex; only a front side exists
            "test:0",
            tmp_path,
            EventWriter(enabled=False),
            lambda p, o: None,
        )

    warnings = [
        r
        for r in caplog.records
        if r.levelno >= 30 and "backs will not be scanned" in r.message
    ]
    assert len(warnings) == 1


def test_a_staging_file_is_never_swept_into_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupted scanimage leaves its in-progress raster beside the
    # completed pages as page_NNNN.pnm.part (measured on sane-backends
    # 1.4.0). It is not a page: never delivered, never counted, and never
    # the frame a later segment numbers past.
    (tmp_path / "page_0001.pnm").write_bytes(b"P4\n1 1\n\x00")  # never announced
    (tmp_path / "page_0002.pnm.part").write_bytes(b"P4\n1 1\n")  # incomplete
    monkeypatch.setattr(
        "scanmole.scanner.probe_capabilities",
        lambda device, settings=(): {
            "resolution": Capability(kind="range", minimum=50, maximum=600)
        },
    )
    monkeypatch.setattr(
        "scanmole.scanner.run_scanimage", lambda command, on_page: (7, "")
    )
    seen: list[Path] = []

    result = scan_to_files(
        _config(),
        "test:0",
        tmp_path,
        EventWriter(enabled=False),
        lambda p, o: seen.append(p),
    )

    assert [page.name for page in result.pages] == ["page_0001.pnm"]
    assert seen == result.pages
    assert (tmp_path / "page_0002.pnm.part").exists()  # left exactly as found
