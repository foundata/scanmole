"""Characterization tests for the reusable GTK form widgets.

These pin the widget-level semantics (selection, availability blocking,
combo helpers) so the controller decomposition cannot drift them. They
need PyGObject and a display; the release matrix venvs skip them like
the other GTK-bound tests.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any

import pytest

pytestmark = [
    pytest.mark.skipif(
        importlib.util.find_spec("gi") is None
        or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="needs PyGObject and a display",
    ),
    # gi's own import noise, exactly like the other GTK-bound tests.
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


@pytest.fixture(scope="module")
def adw() -> Any:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    return Adw


ITEMS = (("First", "first"), ("Second", "second"), ("Third", "third"))


@pytest.fixture(params=["toggles", "combo"])
def choice_backend(request: Any, adw: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run ChoiceRow tests on both backends.

    The ComboRow fallback serves libadwaita < 1.7 (Ubuntu 24.04); it is
    forced here by hiding ToggleGroup from the widgets module.
    """
    if request.param == "combo":
        import scanmole_gui.widgets as widgets_mod

        class FallbackAdw:
            def __getattr__(self, name: str) -> Any:
                if name == "ToggleGroup":
                    raise AttributeError(name)
                return getattr(adw, name)

        monkeypatch.setattr(widgets_mod, "Adw", FallbackAdw())
    return str(request.param)


def _choice_row(adw: Any, **kwargs: Any) -> Any:
    from scanmole_gui.app import ChoiceRow

    group = adw.PreferencesGroup()
    return ChoiceRow(group, "Title", ITEMS, **kwargs)


def _click_choice(row: Any, index: int) -> None:
    """Activate an item the way a user click does, on either backend."""
    if row._toggles is not None:
        row._toggles.set_active(index)
    else:
        row._combo.set_selected(index)


def test_choice_row_defaults_to_the_first_item(adw: Any, choice_backend: str) -> None:
    row = _choice_row(adw)

    assert row.value() == "first"
    assert row.blocked_reason() is None


def test_choice_row_select_switches_and_fires_on_change(
    adw: Any, choice_backend: str
) -> None:
    changes: list[str] = []
    row: Any = _choice_row(adw, on_change=lambda: changes.append(row.value()))

    row.select("second")

    assert row.value() == "second"
    assert changes == ["second"]
    row.select("missing")  # unknown values are a no-op
    assert row.value() == "second"


def test_choice_row_keeps_a_blocked_active_selection(
    adw: Any, choice_backend: str
) -> None:
    # The saved choice turns out blocked on the probed scanner: it must
    # stay selected and visible with its reason, so the window can
    # disable Start instead of the widget silently switching to the
    # first item (Adw.ToggleGroup clears a disabled active toggle).
    row = _choice_row(adw)
    row.select("second")
    row.set_availability({"second": "unavailable", "third": "missing"})

    assert row.value() == "second"
    assert row.blocked_reason() == "unavailable"

    row.set_availability({})
    assert row.blocked_reason() is None


def test_choice_row_click_back_onto_a_blocked_value_reverts(
    adw: Any, choice_backend: str
) -> None:
    # The active-but-blocked toggle stays clickable; returning to it
    # after choosing an available value reverts and reports the reason,
    # exactly like the ComboRow fallback.
    changes: list[str] = []
    blocked_calls: list[tuple[str, str]] = []
    row: Any = _choice_row(adw, on_change=lambda: changes.append(row.value()))
    row.select("second")
    row.set_availability(
        {"second": "unavailable"},
        lambda value, reason: blocked_calls.append((value, reason)),
    )

    changes.clear()
    _click_choice(row, 2)  # the user picks the available "third"
    assert row.value() == "third"
    assert changes == ["third"]

    changes.clear()
    _click_choice(row, 1)  # and clicks back onto the blocked value

    assert row.value() == "third"  # reverted, never adopted
    assert blocked_calls == [("second", "unavailable")]
    # Adw.ToggleGroup delivers the revert's notify deferred, so a single
    # echo of the KEPT value may fire; the blocked value never does.
    assert changes in ([], ["third"])


def test_combo_helpers_round_trip_values(adw: Any) -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from scanmole_gui.app import combo_select, combo_value

    row = adw.ComboRow()
    row.set_model(Gtk.StringList.new([label for label, _value in ITEMS]))

    assert combo_value(row, ITEMS) == "first"
    combo_select(row, ITEMS, "third")
    assert combo_value(row, ITEMS) == "third"
    combo_select(row, ITEMS, "missing")  # unknown values keep the selection
    assert combo_value(row, ITEMS) == "third"


def test_plain_string_factory_builds_a_factory(adw: Any) -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from scanmole_gui.app import plain_string_factory

    assert isinstance(plain_string_factory(), Gtk.SignalListItemFactory)


def test_pure_helpers() -> None:
    from scanmole_gui.app import abbreviate_home, as_int

    assert as_int(7, 3) == 7
    assert as_int("7", 3) == 3
    assert as_int(None, 3) == 3

    home = os.path.expanduser("~")
    assert abbreviate_home(home) == "~"
    assert abbreviate_home(f"{home}/scans") == "~/scans"
    assert abbreviate_home(f"{home}rest") == f"{home}rest"  # no separator
    assert abbreviate_home("/tmp/scans") == "/tmp/scans"
