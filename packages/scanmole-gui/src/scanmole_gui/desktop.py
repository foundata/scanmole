"""Desktop-entry and icon installation for the GUI (no GTK).

Owns the deterministic construction, installation and removal of the
freedesktop.org desktop entry and the application icon. Every path and
the pinned executable are injected by the caller; the window keeps the
GLib/XDG path discovery and all user-facing dialogs.

Installing the entry is a deliberate action in the settings dialog, not
a startup side effect: with uv-managed environments the executable path
is not stable, so pinning it (and refreshing it after the environment
moved) is the user's call. A future RPM ships system-wide files instead.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def exec_value(executable: str) -> str:
    """The desktop-entry ``Exec`` value for one executable path.

    Readers unescape the value twice per the freedesktop.org spec: the
    key-file string-value rule first, then the double-quote rule of the
    Exec field. Writing therefore escapes in the reverse order: quote
    the argument (escaping backslash, double quote, backtick and dollar
    sign), then double every backslash for the string layer (a literal
    backslash becomes four), and double literal percents so they cannot
    be read as field codes.

    Raises:
        ValueError: For a path the spec cannot express: the executable
            name may not contain an equal sign, and control characters
            cannot survive a key-file line.
    """
    if "=" in executable:
        raise ValueError("the executable path must not contain an equal sign")
    if any(ord(character) < 0x20 for character in executable):
        raise ValueError("the executable path must not contain control characters")
    quoted = executable
    for character in ("\\", '"', "`", "$"):
        quoted = quoted.replace(character, "\\" + character)
    return f'"{quoted}"'.replace("\\", "\\\\").replace("%", "%%")


def render_desktop_entry(executable: str, app_id: str) -> str:
    """The exact desktop-entry text pinning ``executable``."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ScanMole\n"
        "Comment=Scan documents from a SANE scanner straight to a "
        "searchable PDF\n"
        "Comment[de]=Scannt Dokumente von einem SANE-Scanner direkt in "
        "ein durchsuchbares PDF\n"
        f"Exec={exec_value(executable)}\n"
        f"Icon={app_id}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "Categories=Office;Scanning;\n"
    )


def _write_atomic(path: Path, data: bytes) -> None:
    """Stage-and-replace write; a failure leaves any existing file intact."""
    staging = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(data)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def ensure_icon(source: Path, target: Path) -> None:
    """Copy or refresh the application icon, best effort.

    The non-intrusive half of the desktop integration: the icon alone
    changes nothing until a desktop entry exists, but keeps an installed
    entry's icon current across package updates.
    """
    try:
        if not source.is_file():
            return
        icon_data = source.read_bytes()
        if not target.is_file() or target.read_bytes() != icon_data:
            _write_atomic(target, icon_data)
    except OSError:
        LOGGER.debug("icon installation skipped", exc_info=True)  # a convenience


def install_desktop_entry(
    entry_path: Path,
    executable: str,
    app_id: str,
    icon_source: Path,
    icon_target: Path,
) -> bool:
    """Write the desktop entry (and refresh the icon); idempotent.

    Returns:
        Whether the entry is in place afterwards. A failed write, or an
        executable path the spec cannot express, leaves any previously
        installed entry untouched.
    """
    try:
        ensure_icon(icon_source, icon_target)
        desktop_entry = render_desktop_entry(executable, app_id)
        if (
            not entry_path.is_file()
            or entry_path.read_text(encoding="utf-8") != desktop_entry
        ):
            _write_atomic(entry_path, desktop_entry.encode("utf-8"))
        return True
    except (OSError, ValueError):
        return False


def remove_desktop_entry(entry_path: Path) -> bool:
    """Delete the desktop entry (the icon may stay; it is inert)."""
    try:
        entry_path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
