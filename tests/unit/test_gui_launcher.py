"""Tests for the scanmole-gui launcher's GTK-free code paths."""

from __future__ import annotations

import pytest

from scanmole_gui import incompatible_cli, main


@pytest.mark.parametrize(
    ("gui", "cli", "needed"),
    [
        ("0.3.0", "0.3.0", None),  # pre-1.0: exact match required
        ("0.3.0", "0.3.1", "0.3.0"),  # pre-1.0: even a patch bump refuses
        ("0.3.0", None, "0.3.0"),  # no hello handshake at all
        ("1.2.0", "1.2.0", None),  # its own release: compatible
        ("1.2.3", "1.9.0", None),  # older GUI may drive a newer CLI
        ("1.2.0", "1.1.0", "1.2.0 or a newer 1.x"),  # newer GUI refuses older
        ("1.2.3", "1.2.2", "1.2.3 or a newer 1.x"),  # even a patch behind
        ("1.2.3", "2.2.3", "1.2.3 or a newer 1.x"),  # major mismatch refuses
        ("2.0.0", "1.9.9", "2.0.0 or a newer 2.x"),
        ("1.2.3", None, "1.2.3 or a newer 1.x"),  # missing version refuses
        ("1.2.3", "garbage", "1.2.3 or a newer 1.x"),  # unparsable refuses
    ],
)
def test_incompatible_cli_is_directional_within_a_major(
    gui: str, cli: str | None, needed: str | None
) -> None:
    assert incompatible_cli(gui, cli) == needed


def test_forced_install_with_an_older_engine_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pip --force or downgraded engine library must produce one clean
    # error line before any GTK or app import, never an import traceback.
    import scanmole

    monkeypatch.setattr(scanmole, "__version__", "0.9.0")

    assert main([]) == 1

    err = capsys.readouterr().err
    assert "scanmole engine" in err
    assert "0.9.0" in err
    assert "Traceback" not in err


def test_gui_version_output_credits_foundata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --version is answered before the GTK probe, so it must work (and be
    # testable) without PyGObject or a display.
    assert main(["--version"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("scanmole-gui ")
    assert "by foundata (https://foundata.com)" in out
