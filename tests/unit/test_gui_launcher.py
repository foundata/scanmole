"""Tests for the scanmole-gui launcher's GTK-free code paths."""

from __future__ import annotations

import pytest

from scanmole.gui import incompatible_cli, main


@pytest.mark.parametrize(
    ("gui", "cli", "needed"),
    [
        ("0.3.0", "0.3.0", None),  # pre-1.0: exact match required
        ("0.3.0", "0.3.1", "0.3.0"),  # pre-1.0: even a patch bump refuses
        ("0.3.0", None, "0.3.0"),  # no hello handshake at all
        ("1.2.3", "1.9.0", None),  # same major: compatible both ways
        ("1.9.0", "1.2.3", None),
        ("1.2.3", "2.2.3", "1.x"),  # major mismatch refuses
        ("2.0.0", "1.9.9", "2.x"),
        ("1.2.3", None, "1.x"),  # missing version refuses
        ("1.2.3", "garbage", "1.x"),  # unparsable version refuses
    ],
)
def test_incompatible_cli_enforces_the_major_version_rule(
    gui: str, cli: str | None, needed: str | None
) -> None:
    assert incompatible_cli(gui, cli) == needed


def test_gui_version_output_credits_foundata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --version is answered before the GTK probe, so it must work (and be
    # testable) without PyGObject or a display.
    assert main(["--version"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("scanmole-gui ")
    assert "by foundata (https://foundata.com)" in out
