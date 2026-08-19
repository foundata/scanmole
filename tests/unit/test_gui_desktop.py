"""Tests for the GTK-free desktop-entry and icon installation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scanmole_gui.desktop import (
    exec_value,
    install_desktop_entry,
    remove_desktop_entry,
    render_desktop_entry,
)

APP_ID = "com.example.TestApp"


def test_desktop_entry_renders_exactly() -> None:
    rendered = render_desktop_entry("/opt/bin/scanmole-gui", APP_ID)

    assert rendered == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ScanMole\n"
        "Comment=Scan documents from a SANE scanner straight to a "
        "searchable PDF\n"
        "Comment[de]=Scannt Dokumente von einem SANE-Scanner direkt in "
        "ein durchsuchbares PDF\n"
        'Exec="/opt/bin/scanmole-gui"\n'
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "Categories=Office;Scanning;\n"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/plain/scanmole-gui", '"/plain/scanmole-gui"'),
        ("/with space/scanmole-gui", '"/with space/scanmole-gui"'),
        # The spec unescapes twice (string value first, then the quoting
        # rule), so writing escapes twice: one literal backslash becomes
        # four, and the quoting backslash before " ` $ is itself doubled.
        ('/odd"quote/gui', '"/odd\\\\"quote/gui"'),
        ("/back\\slash/gui", '"/back\\\\\\\\slash/gui"'),
        ("/dollar$HOME/gui", '"/dollar\\\\$HOME/gui"'),
        ("/tick`id`/gui", '"/tick\\\\`id\\\\`/gui"'),
        # A literal percent must not be read as a field code.
        ("/per%Uce%nt/gui", '"/per%%Uce%%nt/gui"'),
    ],
)
def test_exec_value_escapes_per_desktop_spec(path: str, expected: str) -> None:
    assert exec_value(path) == expected


@pytest.mark.parametrize("path", ["/env=like/gui", "/line\nbreak/gui", "/tab\tgui"])
def test_exec_value_rejects_unrepresentable_executables(path: str) -> None:
    # The spec forbids an equal sign in the executable name, and control
    # characters cannot survive a key-file line.
    with pytest.raises(ValueError, match="executable"):
        exec_value(path)


def test_install_refuses_an_unrepresentable_executable(tmp_path: Path) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)

    assert not install_desktop_entry(
        entry, "/env=like/gui", APP_ID, icon_source, icon_target
    )
    assert not entry.exists()


_NEEDS_VALIDATOR = pytest.mark.skipif(
    shutil.which("desktop-file-validate") is None,
    reason="needs desktop-file-validate",
)


@_NEEDS_VALIDATOR
@pytest.mark.parametrize(
    "path",
    [
        "/usr/local/bin/scanmole-gui",
        "/with space/scanmole-gui",
        "/back\\slash/gui",
        '/odd"quote/gui',
        "/dollar$HOME/gui",
        "/tick`id`/gui",
        "/per%Uce%Qnt/gui",
        "/semi;colon&and~more/gui",
    ],
)
def test_rendered_entry_passes_desktop_file_validate(tmp_path: Path, path: str) -> None:
    entry = tmp_path / f"{APP_ID}.desktop"
    entry.write_text(render_desktop_entry(path, APP_ID), encoding="utf-8")

    result = subprocess.run(
        ["desktop-file-validate", str(entry)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    entry = tmp_path / "applications" / f"{APP_ID}.desktop"
    icon_source = tmp_path / "packaged.svg"
    icon_target = tmp_path / "icons" / f"{APP_ID}.svg"
    return entry, icon_source, icon_target


def test_install_writes_entry_and_icon_idempotently(tmp_path: Path) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)
    icon_source.write_bytes(b"<svg/>")

    assert install_desktop_entry(entry, "/x/gui", APP_ID, icon_source, icon_target)
    first_entry = entry.read_bytes()
    first_icon = icon_target.read_bytes()
    assert install_desktop_entry(entry, "/x/gui", APP_ID, icon_source, icon_target)

    assert entry.read_bytes() == first_entry
    assert icon_target.read_bytes() == first_icon == b"<svg/>"


def test_install_refreshes_a_changed_executable(tmp_path: Path) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)

    install_desktop_entry(entry, "/old/gui", APP_ID, icon_source, icon_target)
    install_desktop_entry(entry, "/new/gui", APP_ID, icon_source, icon_target)

    assert 'Exec="/new/gui"' in entry.read_text(encoding="utf-8")


def test_install_without_a_packaged_icon_still_writes_the_entry(
    tmp_path: Path,
) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)

    assert install_desktop_entry(entry, "/x/gui", APP_ID, icon_source, icon_target)
    assert entry.is_file()
    assert not icon_target.exists()


def test_failed_write_preserves_the_installed_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)
    install_desktop_entry(entry, "/old/gui", APP_ID, icon_source, icon_target)
    original = entry.read_bytes()
    real_write = Path.write_bytes

    def failing_write(self: Path, data: bytes) -> int:
        if self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    assert not install_desktop_entry(
        entry, "/new/gui", APP_ID, icon_source, icon_target
    )
    assert entry.read_bytes() == original
    assert list((tmp_path / "applications").iterdir()) == [entry]  # no staging


def test_remove_is_idempotent(tmp_path: Path) -> None:
    entry, icon_source, icon_target = _paths(tmp_path)
    install_desktop_entry(entry, "/x/gui", APP_ID, icon_source, icon_target)

    assert remove_desktop_entry(entry)
    assert not entry.exists()
    assert remove_desktop_entry(entry)  # already gone: still fine
