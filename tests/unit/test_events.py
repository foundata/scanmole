"""Tests for the JSON-lines event protocol writer."""

from __future__ import annotations

import io
import json

from scanmole.events import EventWriter


def test_emit_writes_one_json_object_per_line() -> None:
    stream = io.StringIO()
    writer = EventWriter(enabled=True, stream=stream)

    writer.emit("start", device="airscan:e0", output="out.pdf")
    writer.emit("done", output="out.pdf", pages=3)

    lines = stream.getvalue().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"event": "start", "device": "airscan:e0", "output": "out.pdf"},
        {"event": "done", "output": "out.pdf", "pages": 3},
    ]


def test_event_key_is_emitted_first() -> None:
    stream = io.StringIO()
    writer = EventWriter(enabled=True, stream=stream)

    writer.emit("page", n=1, blank=False)

    assert stream.getvalue().startswith('{"event": "page"')


def test_disabled_writer_emits_nothing() -> None:
    stream = io.StringIO()
    writer = EventWriter(enabled=False, stream=stream)

    writer.emit("start")
    writer.error("boom")

    assert stream.getvalue() == ""


def test_error_emits_error_event() -> None:
    stream = io.StringIO()
    writer = EventWriter(enabled=True, stream=stream)

    writer.error("device offline")

    assert json.loads(stream.getvalue()) == {
        "event": "error",
        "message": "device offline",
    }
