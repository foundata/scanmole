"""Tests for argument-to-config translation, path resolution and exit codes."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path

import pytest

from scanmole.cli import _build_config, _resolve_output, _Terminated, build_parser, main
from scanmole.config import ScanConfig
from scanmole.errors import InputError, ProcessingError


def _raiser(exc: BaseException) -> object:
    def raise_it(config: object, events: object) -> int:
        raise exc

    return raise_it


def _parse(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def test_resolve_output_appends_pdf_suffix(tmp_path: Path) -> None:
    args = _parse([str(tmp_path / "invoice")])

    assert _resolve_output(args).name == "invoice.pdf"


def test_resolve_output_avoids_overwriting_existing_file(tmp_path: Path) -> None:
    existing = tmp_path / "scan.pdf"
    existing.touch()
    args = _parse([str(existing)])

    assert _resolve_output(args).name == "scan_2.pdf"


def test_resolve_output_reserves_the_name_against_concurrent_runs(
    tmp_path: Path,
) -> None:
    # Two runs resolving the same name must never get the same path: the
    # first call reserves the file on disk, so the second sees it taken. A
    # bare existence check would hand both runs "scan.pdf".
    args = _parse([str(tmp_path / "scan")])

    first = _resolve_output(args)
    second = _resolve_output(_parse([str(tmp_path / "scan")]))

    assert first.name == "scan.pdf"
    assert second.name == "scan_2.pdf"
    assert first.is_file() and first.stat().st_size == 0


def test_resolve_output_rejects_an_unwritable_location(tmp_path: Path) -> None:
    args = _parse([str(tmp_path / "missing-dir" / "scan.pdf")])

    with pytest.raises(InputError, match="cannot create output file"):
        _resolve_output(args)


def test_main_removes_the_reservation_when_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scanmole.cli.run_pipeline", _raiser(ProcessingError("ocrmypdf failed"))
    )
    output = tmp_path / "doomed.pdf"

    assert main(["-o", str(output)]) == 5

    assert not output.exists()


def test_main_removes_the_reservation_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scanmole.cli.run_pipeline", _raiser(KeyboardInterrupt()))
    output = tmp_path / "interrupted.pdf"

    assert main(["-o", str(output)]) == 130

    assert not output.exists()


def test_main_keeps_the_published_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_pipeline(config: ScanConfig, events: object) -> int:
        config.output.write_bytes(b"%PDF-fake")
        return 0

    monkeypatch.setattr("scanmole.cli.run_pipeline", fake_pipeline)
    output = tmp_path / "done.pdf"

    assert main(["-o", str(output)]) == 0

    assert output.read_bytes() == b"%PDF-fake"


def test_resolve_output_rejects_output_and_positional_together(tmp_path: Path) -> None:
    args = _parse(["-o", str(tmp_path / "a.pdf"), "base"])

    with pytest.raises(InputError, match="either -o/--output or a positional"):
        _resolve_output(args)


def test_resolve_output_default_name_has_scan_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    args = _parse([])

    resolved = _resolve_output(args)

    assert resolved.name.endswith(".pdf")
    assert "_scan_" in resolved.name


def test_build_config_maps_flags(tmp_path: Path) -> None:
    args = _parse(
        [
            "--source",
            "flatbed",
            "--mode",
            "gray",
            "-r",
            "150",
            "--no-ocr",
            "--keep-blanks",
            "-o",
            str(tmp_path / "out.pdf"),
        ]
    )

    config = _build_config(args)

    assert config.source == "flatbed"
    assert config.mode == "gray"
    assert config.resolution == 150
    assert config.ocr is False
    assert config.keep_blanks is True
    assert config.output == (tmp_path / "out.pdf").resolve()


def test_lineart_threshold_defaults_to_half(tmp_path: Path) -> None:
    config = _build_config(_parse(["-o", str(tmp_path / "a.pdf")]))

    assert config.lineart_threshold == 0.5


def test_lineart_threshold_rejects_values_of_one_or_more(tmp_path: Path) -> None:
    args = _parse(["--lineart-threshold", "1.5", "-o", str(tmp_path / "a.pdf")])

    with pytest.raises(InputError, match="--lineart-threshold"):
        _build_config(args)


def test_lineart_threshold_zero_is_accepted_as_off(tmp_path: Path) -> None:
    args = _parse(["--lineart-threshold", "0", "-o", str(tmp_path / "a.pdf")])

    assert _build_config(args).lineart_threshold == 0


def test_pdfa_defaults_on_and_can_be_disabled(tmp_path: Path) -> None:
    default = _build_config(_parse(["-o", str(tmp_path / "a.pdf")]))
    disabled = _build_config(_parse(["--no-pdfa", "-o", str(tmp_path / "b.pdf")]))

    assert default.pdfa is True
    assert disabled.pdfa is False


def test_build_config_from_images_are_paths(tmp_path: Path) -> None:
    args = _parse(["--from-images", "a.png", "b.png", "-o", str(tmp_path / "o.pdf")])

    config = _build_config(args)

    assert config.from_images == (Path("a.png"), Path("b.png"))


def test_from_images_rejects_an_explicit_device(tmp_path: Path) -> None:
    args = _parse(
        ["--from-images", "a.png", "-d", "test:0", "-o", str(tmp_path / "o.pdf")]
    )

    with pytest.raises(InputError, match="--from-images"):
        _build_config(args)


def test_from_images_ignores_the_device_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCANMOLE_DEVICE", "test:0")

    config = _build_config(
        _parse(["--from-images", "a.png", "-o", str(tmp_path / "o.pdf")])
    )

    assert config.device is None


def test_main_maps_domain_errors_to_their_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "scanmole.cli.run_pipeline", _raiser(ProcessingError("ocrmypdf failed"))
    )

    assert main(["--json"]) == 5

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event == {"event": "error", "message": "ocrmypdf failed", "code": 5}


def test_main_returns_130_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scanmole.cli.run_pipeline", _raiser(KeyboardInterrupt()))

    assert main([]) == 130


def test_main_returns_143_on_sigterm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scanmole.cli.run_pipeline", _raiser(_Terminated()))

    assert main(["--json"]) == 143

    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event == {"event": "error", "message": "terminated", "code": 143}


def test_main_installs_a_sigterm_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scanmole.cli.run_pipeline", lambda config, events: 0)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        main([])
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler is not previous
        with pytest.raises(_Terminated):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
