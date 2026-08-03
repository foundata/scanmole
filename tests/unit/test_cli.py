"""Tests for argument-to-config translation, path resolution and exit codes."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path

import pytest

from scanmole.cli import _build_config, _resolve_output, _Terminated, build_parser, main
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


def test_build_config_from_images_are_paths(tmp_path: Path) -> None:
    args = _parse(["--from-images", "a.png", "b.png", "-o", str(tmp_path / "o.pdf")])

    config = _build_config(args)

    assert config.from_images == (Path("a.png"), Path("b.png"))


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
