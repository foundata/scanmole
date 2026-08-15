"""Tests for the scanmole-gui launcher's GTK-free code paths."""

from __future__ import annotations

import pytest

from scanmole.gui import main


def test_gui_version_output_credits_foundata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --version is answered before the GTK probe, so it must work (and be
    # testable) without PyGObject or a display.
    assert main(["--version"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("scanmole-gui ")
    assert "by foundata (https://foundata.com)" in out
