"""Tests for the scanmole-gui launcher's GTK-free code paths."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from scanmole_gui import incompatible_cli, main

pytestmark = [
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::UserWarning"),
]

_NEEDS_GI = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None,
    reason="needs PyGObject (scanmole_gui.app imports gi)",
)


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


@_NEEDS_GI
def test_the_engine_is_found_beside_the_gui_without_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A desktop launcher passes the session environment, not the shell's,
    # so a GUI installed into a virtual environment sees no PATH entry for
    # its own engine. The engine is right beside it.
    from scanmole_gui.app import find_scanmole

    installed = tmp_path / "bin"
    installed.mkdir()
    engine = installed / "scanmole"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)

    monkeypatch.setattr(sys, "argv", [str(installed / "scanmole-gui")])
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv("PATH", "/nonexistent")

    assert find_scanmole() == str(engine)

    # Or beside the interpreter running it, which is where a venv's
    # console scripts live.
    monkeypatch.setattr(sys, "argv", ["scanmole-gui"])
    monkeypatch.setattr(sys, "executable", str(installed / "python3"))
    assert find_scanmole() == str(engine)


@_NEEDS_GI
def test_a_system_installation_still_comes_from_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scanmole_gui.app import find_scanmole

    elsewhere = tmp_path / "usr-bin"
    elsewhere.mkdir()
    engine = elsewhere / "scanmole"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    lonely = tmp_path / "gui-only"
    lonely.mkdir()

    monkeypatch.setattr(sys, "argv", [str(lonely / "scanmole-gui")])
    monkeypatch.setattr(sys, "executable", str(lonely / "python3"))
    monkeypatch.setenv("PATH", str(elsewhere))

    assert find_scanmole() == str(engine)

    monkeypatch.setenv("PATH", "/nonexistent")
    assert find_scanmole() == "scanmole"  # nothing anywhere: the bare name

    # PATH still decides where it answers, so an explicitly placed engine
    # is never silently overridden by a neighbour.
    neighbour = lonely / "scanmole"
    neighbour.write_text("#!/bin/sh\n")
    neighbour.chmod(0o755)
    monkeypatch.setenv("PATH", str(elsewhere))
    assert find_scanmole() == str(engine)


@_NEEDS_GI
def test_a_bare_argv_name_never_reaches_into_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolving a bare "scanmole-gui" would point at the current
    # directory, where a stray file must not become the engine.
    from scanmole_gui.app import find_scanmole

    stray = tmp_path / "scanmole"
    stray.write_text("#!/bin/sh\n")
    stray.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scanmole-gui"])
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv("PATH", "/nonexistent")

    assert find_scanmole() == "scanmole"
