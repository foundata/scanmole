"""Tests for the output filename template expansion."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from scanmole.naming import (
    DEFAULT_OUTPUT_TEMPLATE,
    expand_template,
    has_counter,
    sanitize_component,
)

WHEN = datetime(2026, 8, 15, 20, 26, 7)


def _expand(template: str, counter: int = 1, device: str | None = "test:0") -> str:
    return expand_template(template, when=WHEN, counter=counter, device=device)


def test_default_template_expands_to_dated_counter_name() -> None:
    assert _expand(DEFAULT_OUTPUT_TEMPLATE) == "2026-08-15_scan_001.pdf"


def test_iso_casing_separates_month_from_minutes() -> None:
    assert _expand("{YYYY}-{MM}-{DD}_{hh}-{mm}-{ss}.pdf") == "2026-08-15_20-26-07.pdf"


def test_adjacent_tokens_expand() -> None:
    assert _expand("{YYYY}{MM}{DD}_{hh}{mm}{ss}_{NN}.pdf") == "20260815_202607_01.pdf"


def test_counter_width_follows_the_number_of_ns() -> None:
    assert _expand("scan_{N}.pdf", counter=7) == "scan_7.pdf"
    assert _expand("scan_{NN}.pdf", counter=7) == "scan_07.pdf"
    assert _expand("scan_{NNN}.pdf", counter=7) == "scan_007.pdf"
    assert _expand("scan_{NN}.pdf", counter=123) == "scan_123.pdf"


def test_unbraced_tokens_stay_literal() -> None:
    # Only braced placeholders expand; plain text is always safe.
    assert _expand("YYYY-MM-DD_scan_NNN.pdf") == "YYYY-MM-DD_scan_NNN.pdf"
    assert _expand("Kasse_Sommer_SCANNER.pdf") == "Kasse_Sommer_SCANNER.pdf"


def test_device_placeholder_is_sanitized() -> None:
    expanded = _expand("{device}.pdf", device="airscan:e0:Brother ADS-4550W (USB)")

    assert expanded == "airscan-e0-Brother-ADS-4550W-USB.pdf"


def test_device_placeholder_without_a_device_raises() -> None:
    with pytest.raises(ValueError, match=r"\{device\}"):
        _expand("{device}.pdf", device=None)


def test_unknown_braced_tokens_stay_literal() -> None:
    assert _expand("{foo}_{NX}_{NN}.pdf") == "{foo}_{NX}_01.pdf"


def test_has_counter_matches_braced_counters_only() -> None:
    assert has_counter("scan_{N}.pdf") is True
    assert has_counter("scan_{NN}.pdf") is True
    assert has_counter("{YYYY}_{NNN}.pdf") is True
    assert has_counter("scan_NN.pdf") is False
    assert has_counter("invoice.pdf") is False


def test_sanitize_component_keeps_safe_characters_only() -> None:
    assert sanitize_component("v4l:/dev/video0") == "v4l-dev-video0"
    assert sanitize_component("...") == "unknown"


def _candidates(template: str, count: int, **kwargs: object) -> list[str]:
    from scanmole.naming import output_candidates

    when = kwargs.pop("when", None) or datetime(2026, 8, 23, 14, 5, 9)
    device = kwargs.pop("device", None)
    stream = output_candidates(template, when=when, device=device)  # type: ignore[arg-type]
    return [next(stream).name for _ in range(count)]


def test_a_counter_template_numbers_its_candidates_from_one() -> None:
    assert _candidates("{YYYY}_scan_{NNN}.pdf", 3) == [
        "2026_scan_001.pdf",
        "2026_scan_002.pdf",
        "2026_scan_003.pdf",
    ]


def test_a_template_without_a_counter_falls_back_to_a_suffix() -> None:
    assert _candidates("report.pdf", 3) == [
        "report.pdf",
        "report_2.pdf",
        "report_3.pdf",
    ]


def test_candidates_end_in_pdf_and_expand_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scanmole.naming import output_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    when = datetime(2026, 8, 23, 14, 5, 9)

    stream = output_candidates("~/scans/report", when=when, device=None)
    first = next(stream)

    assert first == tmp_path / "scans" / "report.pdf"  # suffix added, ~ expanded
    assert next(stream).name == "report_2.pdf"


def test_candidates_sanitize_the_device_and_refuse_a_missing_one() -> None:
    assert _candidates("{device}_{N}.pdf", 1, device="epsonds:net:10.0.0.2") == [
        "epsonds-net-10.0.0.2_1.pdf"
    ]
    with pytest.raises(ValueError, match=r"\{device\}"):
        _candidates("{device}.pdf", 1)


def test_one_timestamp_serves_a_whole_candidate_search() -> None:
    # The search must not straddle a second boundary: every candidate in
    # one search carries the timestamp the search started with.
    from scanmole.naming import output_candidates

    when = datetime(2026, 8, 23, 14, 5, 9)
    stream = output_candidates("{hh}{mm}{ss}_{NN}.pdf", when=when, device=None)

    assert [next(stream).name for _ in range(3)] == [
        "140509_01.pdf",
        "140509_02.pdf",
        "140509_03.pdf",
    ]


def test_ordinary_paths_keep_their_established_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scanmole.naming import as_pdf_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()

    assert as_pdf_path("report") == tmp_path / "report.pdf"  # relative, cwd-anchored
    assert as_pdf_path("sub/../report.PDF") == tmp_path / "report.PDF"
    assert as_pdf_path(str(tmp_path / "sub" / "x.pdf")) == tmp_path / "sub" / "x.pdf"


def test_concurrent_reservations_still_never_collide(tmp_path: Path) -> None:
    # The reservation is what makes two runs pick different names, and it
    # is unchanged: only the final component stopped being resolved.
    from scanmole.cli import _resolve_output

    args = type(
        "Args", (), {"output": str(tmp_path / "batch_{NN}.pdf"), "outbase": None}
    )()
    reserved = [_resolve_output(args, None) for _ in range(5)]

    assert len(set(reserved)) == 5
    assert [path.name for path in reserved] == [
        f"batch_{index:02d}.pdf" for index in range(1, 6)
    ]
