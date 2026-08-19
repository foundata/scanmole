"""Persisted GUI settings: tolerant loading and atomic storing (no GTK).

Owns the JSON representation of ``gui.json`` and its file lifecycle. The
window supplies the path (it knows the GLib config directory) and keeps
all widget reads, widget updates and translations; this module never
discovers platform paths itself.

The format is deliberately forgiving: a missing file is a first start, a
malformed or wrong-shaped file starts fresh instead of crashing, and
unknown keys survive a load/store round trip so newer GUIs' settings are
not destroyed by older ones.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def load_settings(path: Path) -> dict[str, object]:
    """Load persisted GUI settings, returning an empty dict on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}  # first start
    except (OSError, ValueError):
        LOGGER.debug("settings unreadable; starting fresh", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def store_settings(path: Path, data: dict[str, object]) -> None:
    """Persist GUI settings; failures only cost this snapshot.

    Written to a sibling file and renamed atomically: an interrupted
    in-place write would leave invalid JSON behind, silently resetting
    every preference on the next launch. A failed write never damages
    the existing file, and the staging file never survives.
    """
    staging = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, path)
    except OSError:
        LOGGER.debug("could not persist settings", exc_info=True)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
