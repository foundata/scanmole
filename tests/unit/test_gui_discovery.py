"""Tests for the GTK-free device discovery result handling."""

from __future__ import annotations

import json

from scanmole_gui import __version__
from scanmole_gui.discovery import (
    Listing,
    display_name,
    evaluate_listing,
    parse_listing,
    parse_version,
)


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _hello() -> dict[str, object]:
    return {"event": "hello", "version": __version__}


def test_parse_listing_filters_virtual_devices() -> None:
    stdout = _stream(
        _hello(),
        {
            "event": "devices",
            "devices": [
                {"device": "v4l:/dev/video0", "model": "Webcam"},
                {"device": "test:0", "model": "Test"},
                {"device": "airscan:e0:Example", "model": "Example"},
                "not-an-object",
            ],
        },
    )

    hello, devices = parse_listing(stdout)

    assert hello == __version__
    assert [device["device"] for device in devices] == ["airscan:e0:Example"]


def test_parse_listing_tolerates_malformed_and_partial_output() -> None:
    stdout = "\n".join(
        [
            "not json at all",
            "[1, 2, 3]",
            '"a string"',
            json.dumps({"event": "devices"}),  # no devices key
            json.dumps({"event": "devices", "devices": None}),
            "{truncated",
        ]
    )

    hello, devices = parse_listing(stdout)

    assert hello is None
    assert devices == []


def test_parse_listing_ignores_a_non_list_devices_payload() -> None:
    # A malformed devices event must neither crash the parser nor erase
    # the devices an earlier valid event already delivered.
    stdout = _stream(
        _hello(),
        {"event": "devices", "devices": [{"device": "airscan:e0:X", "model": "X"}]},
        {"event": "devices", "devices": 1},
        {"event": "devices", "devices": "airscan:e0:Y"},
    )

    hello, devices = parse_listing(stdout)

    assert hello == __version__
    assert [device["device"] for device in devices] == ["airscan:e0:X"]


def test_parse_listing_drops_non_string_device_fields() -> None:
    # A numeric vendor or model must degrade to a missing field instead
    # of crashing display_name() later. An entry without a usable device
    # identifier is dropped entirely: it would count as a found scanner
    # yet selecting it would scan whatever default the CLI picks.
    stdout = _stream(
        {
            "event": "devices",
            "devices": [
                {"device": "airscan:e0:X", "vendor": 7, "model": "Model"},
                {"device": 42, "vendor": "Vendor", "model": "Other"},
                {"device": "", "model": "Nameless"},
                {"model": "Idless"},
            ],
        },
    )

    _hello_version, devices = parse_listing(stdout)

    assert devices == [{"device": "airscan:e0:X", "model": "Model"}]
    assert display_name(devices[0], "fallback") == "Model"


def test_evaluate_accepts_a_compatible_run() -> None:
    stdout = _stream(
        _hello(),
        {"event": "devices", "devices": [{"device": "airscan:e0:X", "model": "X"}]},
    )

    listing = evaluate_listing(stdout, 0)

    assert listing.needed is None
    assert listing.failed_exit is None
    assert listing.cli_version == __version__
    assert len(listing.devices) == 1
    assert listing.retry is False


def test_evaluate_refuses_an_incompatible_cli_and_drops_its_devices() -> None:
    stdout = _stream(
        {"event": "hello", "version": "0.0.1"},
        {"event": "devices", "devices": [{"device": "x:0", "model": "X"}]},
    )

    listing = evaluate_listing(stdout, 0)

    assert listing.needed is not None
    assert listing.devices == []  # never trust an incompatible protocol
    assert listing.retry is False  # polling cannot fix a version mismatch


def test_evaluate_treats_zero_exit_without_hello_as_incompatible() -> None:
    # A clean run that never says hello predates the handshake: refuse.
    listing = evaluate_listing("", 0)

    assert listing.needed is not None


def test_evaluate_reports_a_plain_failure_without_hello() -> None:
    # A failing run without hello is a broken search, not a version
    # decision; polling may recover it.
    listing = evaluate_listing("garbage", 3)

    assert listing.needed is None
    assert listing.failed_exit == 3
    assert listing.retry is True


def test_evaluate_ignores_the_exit_code_when_devices_arrived() -> None:
    stdout = _stream(
        _hello(),
        {"event": "devices", "devices": [{"device": "x:0", "model": "X"}]},
    )

    listing = evaluate_listing(stdout, 1)

    assert listing.failed_exit is None
    assert len(listing.devices) == 1


def test_empty_result_retries() -> None:
    listing = evaluate_listing(_stream(_hello(), {"event": "devices"}), 0)

    assert listing.devices == []
    assert listing.retry is True


def test_listing_defaults_are_a_retryable_empty_result() -> None:
    assert Listing().retry is True


def test_parse_version_reads_the_first_line() -> None:
    assert parse_version("scanmole 1.2.3\nby example\n") == "1.2.3"
    assert parse_version("scanmole 1.2.3") == "1.2.3"
    assert parse_version("") is None
    assert parse_version("one-token") is None


def test_display_name_prefers_vendor_and_model() -> None:
    assert display_name({"vendor": " ACME ", "model": "Scan 9"}, "?") == "ACME Scan 9"
    assert display_name({"device": "x:0"}, "?") == "x:0"
    assert display_name({}, "Unknown device") == "Unknown device"
