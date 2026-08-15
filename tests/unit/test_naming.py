"""Tests for the output filename template expansion."""

from __future__ import annotations

from datetime import datetime

import pytest

from scanmole.naming import (
    DEFAULT_OUTPUT_TEMPLATE,
    expand_template,
    has_counter,
    sanitize_component,
)

WHEN = datetime(2026, 8, 15, 20, 26, 7)


def _expand(template: str, counter: int = 1, device: str | None = "test:0") -> str:
    return expand_template(
        template, when=WHEN, counter=counter, device=device, preset="lineart-300"
    )


def test_default_template_expands_to_dated_counter_name() -> None:
    assert _expand(DEFAULT_OUTPUT_TEMPLATE) == "2026-08-15_scan_001.pdf"


def test_iso_casing_separates_month_from_minutes() -> None:
    assert _expand("{YYYY}-{MM}-{DD}_{hh}-{mm}-{ss}.pdf") == "2026-08-15_20-26-07.pdf"


def test_adjacent_tokens_expand() -> None:
    assert _expand("{YYYY}{MM}{DD}_{hh}{mm}{ss}_{NN}.pdf") == "20260815_202607_01.pdf"


def test_counter_width_follows_the_number_of_ns() -> None:
    assert _expand("scan_{NN}.pdf", counter=7) == "scan_07.pdf"
    assert _expand("scan_{NNN}.pdf", counter=7) == "scan_007.pdf"
    assert _expand("scan_{NN}.pdf", counter=123) == "scan_123.pdf"


def test_unbraced_tokens_stay_literal() -> None:
    # Only braced placeholders expand; plain text is always safe.
    assert _expand("YYYY-MM-DD_scan_NNN.pdf") == "YYYY-MM-DD_scan_NNN.pdf"
    assert _expand("Kasse_Sommer_SCANNER.pdf") == "Kasse_Sommer_SCANNER.pdf"


def test_preset_placeholder_expands_to_the_settings_slug() -> None:
    assert _expand("{preset}_{NN}.pdf") == "lineart-300_01.pdf"


def test_device_placeholder_is_sanitized() -> None:
    expanded = _expand("{device}.pdf", device="airscan:e0:Brother ADS-4550W (USB)")

    assert expanded == "airscan-e0-Brother-ADS-4550W-USB.pdf"


def test_device_placeholder_without_a_device_raises() -> None:
    with pytest.raises(ValueError, match=r"\{device\}"):
        _expand("{device}.pdf", device=None)


def test_unknown_braced_tokens_stay_literal() -> None:
    assert _expand("{foo}_{N}_{NN}.pdf") == "{foo}_{N}_01.pdf"


def test_has_counter_matches_braced_counters_only() -> None:
    assert has_counter("scan_{NN}.pdf") is True
    assert has_counter("{YYYY}_{NNN}.pdf") is True
    assert has_counter("scan_NN.pdf") is False
    assert has_counter("invoice.pdf") is False


def test_sanitize_component_keeps_safe_characters_only() -> None:
    assert sanitize_component("v4l:/dev/video0") == "v4l-dev-video0"
    assert sanitize_component("...") == "unknown"
