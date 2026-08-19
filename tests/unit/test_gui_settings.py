"""Tests for the GTK-free GUI settings persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scanmole_gui.settings import load_settings, store_settings


def test_missing_file_is_a_first_start(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "gui.json") == {}


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"", b"[1, 2, 3]", b'"just a string"', b"\xff\xfe garbage"],
)
def test_malformed_or_wrong_shaped_files_start_fresh(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "gui.json"
    path.write_bytes(content)

    assert load_settings(path) == {}


def test_unreadable_file_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "gui.json"
    path.write_text("{}")
    path.chmod(0o000)
    try:
        assert load_settings(path) == {}
    finally:
        path.chmod(0o600)


def test_partial_and_unknown_keys_survive_a_round_trip(tmp_path: Path) -> None:
    # Forward compatibility: a newer GUI's keys must not be destroyed by
    # an older one that merely loads and stores.
    path = tmp_path / "gui.json"
    data: dict[str, object] = {
        "source": "adf-duplex",
        "future_key": {"nested": [1, 2]},
        "window_width": 645,
    }
    store_settings(path, data)

    assert load_settings(path) == data


def test_persisted_representation_is_stable(tmp_path: Path) -> None:
    # The on-disk schema is part of the contract: two-space indented JSON
    # with a trailing newline, keys in insertion order.
    path = tmp_path / "gui.json"
    data: dict[str, object] = {"source": "adf", "resolution": "300", "ocr": True}

    store_settings(path, data)

    assert path.read_text(encoding="utf-8") == json.dumps(data, indent=2) + "\n"


def test_store_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "config" / "scanmole" / "gui.json"

    store_settings(path, {"a": 1})

    assert load_settings(path) == {"a": 1}


def test_failed_write_preserves_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "gui.json"
    store_settings(path, {"keep": "me"})
    real_write = Path.write_text

    def failing_write(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing_write)

    store_settings(path, {"lost": "snapshot"})  # must not raise

    assert load_settings(path) == {"keep": "me"}
    assert list(tmp_path.iterdir()) == [path]  # no staging leftovers


def test_failed_replace_preserves_the_original_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "gui.json"
    store_settings(path, {"keep": "me"})

    def failing_replace(src: object, dst: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(os, "replace", failing_replace)

    store_settings(path, {"lost": "snapshot"})

    assert load_settings(path) == {"keep": "me"}
    assert list(tmp_path.iterdir()) == [path]
