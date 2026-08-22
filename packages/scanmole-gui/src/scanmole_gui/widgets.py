"""Reusable GTK form primitives (no ScanMole workflow policy).

Small widgets and helpers the form and dialogs share: a fixed-choice
preferences row with availability blocking, ``(label, value)`` combo
helpers and a dropdown item factory without the default label cap.
Everything here is application-agnostic; workflow policy stays in the
form, window and the GTK-free modules.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402  # after require_version


def combo_value(row: Adw.ComboRow, items: tuple[tuple[str, str], ...]) -> str:
    """Return the CLI value for a combo row's selected ``(label, value)`` item."""
    index = int(row.get_selected())  # untyped GTK call
    return items[index][1] if 0 <= index < len(items) else items[0][1]


def combo_select(
    row: Adw.ComboRow, items: tuple[tuple[str, str], ...], value: str
) -> None:
    """Select the combo row item whose CLI value equals ``value``."""
    for index, (_label, item_value) in enumerate(items):
        if item_value == value:
            row.set_selected(index)
            return


def plain_string_factory() -> Gtk.SignalListItemFactory:
    """A dropdown item factory whose labels take their natural width.

    The default ``Adw.ComboRow`` factory caps labels at about 20 characters
    and ellipsizes, which truncates device names like "Brother ADS-4550W".
    """
    factory = Gtk.SignalListItemFactory()

    def setup(_factory: object, list_item: Gtk.ListItem) -> None:
        list_item.set_child(Gtk.Label(xalign=0.0))

    def bind(_factory: object, list_item: Gtk.ListItem) -> None:
        list_item.get_child().set_label(list_item.get_item().get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


class ChoiceRow:
    """A preferences row choosing one of a few fixed ``(label, value)`` items.

    Renders the options as an inline ``Adw.ToggleGroup`` (all choices visible,
    per the design) when libadwaita provides it (>= 1.7); older platforms
    (e.g. Ubuntu 24.04) fall back to a plain ``Adw.ComboRow`` so the full
    option set stays available everywhere. ``dropdown=True`` picks the
    combo row deliberately, for choices whose labels are too long to read
    as inline toggles; the blocking semantics are identical either way.
    """

    def __init__(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        items: tuple[tuple[str, str], ...],
        on_change: Callable[[], None] | None = None,
        tooltips: tuple[str, ...] | None = None,
        dropdown: bool = False,
    ) -> None:
        """Build the row inside ``group``.

        ``tooltips`` explains the items one by one (empty string = none);
        the dropdown rendering has no per-item tooltips.
        """
        self._items = items
        self._on_change = on_change
        self._blocked: dict[str, str] = {}
        self._on_blocked: Callable[[str, str], None] | None = None
        self._reverting = False
        self._current = items[0][1]
        if hasattr(Adw, "ToggleGroup") and not dropdown:
            self.row: Adw.ActionRow = Adw.ActionRow(title=title)
            self._toggles = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
            for index, (label, _value) in enumerate(items):
                toggle = Adw.Toggle(label=label)
                if tooltips is not None and tooltips[index]:
                    toggle.set_tooltip(tooltips[index])
                self._toggles.add(toggle)
            self._toggles.connect("notify::active", self._changed)
            self.row.add_suffix(self._toggles)
            self._combo: Adw.ComboRow | None = None
        else:
            self._toggles = None
            self._combo = Adw.ComboRow(title=title)
            # The default factory caps labels near 20 characters, which
            # would ellipsize long choices such as the feeder's full name.
            self._combo.set_factory(plain_string_factory())
            self._combo.set_model(Gtk.StringList.new([label for label, _v in items]))
            self._combo.connect("notify::selected", self._changed)
            self.row = self._combo
        group.add(self.row)

    def _changed(self, *_args: object) -> None:
        if self._reverting:
            return
        value = self.value()
        if value in self._blocked:
            # Visible but not selectable: revert to the previous choice and
            # tell the window why (both the ToggleGroup and the ComboRow
            # fallback enforce this identically).
            self._reverting = True
            try:
                self.select(self._current)
            finally:
                self._reverting = False
            if self._on_blocked is not None:
                self._on_blocked(value, self._blocked[value])
            return
        self._current = value
        # The active-toggle exemption travels with the selection: a
        # blocked value the user just left must not keep rendering as
        # clickable.
        self._render_enabled()
        if self._on_change is not None:
            self._on_change()

    def set_availability(
        self,
        blocked: dict[str, str],
        on_blocked: Callable[[str, str], None] | None = None,
    ) -> None:
        """Mark values as not selectable (value -> reason), keep them visible.

        The active selection is not changed here even when it is blocked;
        the caller decides how to surface that (disable Start, show the
        reason) instead of silently switching the user's choice.
        """
        self._blocked = blocked
        self._on_blocked = on_blocked
        self._render_enabled()

    def _render_enabled(self) -> None:
        """Render the block state onto the toggles for the current selection.

        Never disables the active toggle: Adw.ToggleGroup clears a
        disabled active toggle, which would silently change the selection
        to the first item. The blocked current choice stays visible and
        the revert path enforces the block, exactly like the ComboRow
        fallback. Re-run whenever the selection moves, so the exemption
        follows the active toggle instead of sticking to a value the
        selection has left (a reconciled-away saved choice would
        otherwise keep rendering as clickable).
        """
        if self._toggles is None or not hasattr(self._toggles, "get_toggle"):
            return
        current = self.value()
        for index, (_label, value) in enumerate(self._items):
            toggle = self._toggles.get_toggle(index)
            if toggle is not None and hasattr(toggle, "set_enabled"):
                toggle.set_enabled(value not in self._blocked or value == current)

    def blocked_reason(self) -> str | None:
        """The reason the current selection is unavailable, if it is."""
        return self._blocked.get(self.value())

    def value(self) -> str:
        """Return the CLI value of the selected item."""
        if self._toggles is not None:
            index = int(self._toggles.get_active())  # untyped GTK call
        else:
            assert self._combo is not None
            index = int(self._combo.get_selected())  # untyped GTK call
        return (
            self._items[index][1]
            if 0 <= index < len(self._items)
            else (self._items[0][1])
        )

    def select(self, value: str) -> None:
        """Select the item whose CLI value equals ``value`` (no-op if absent)."""
        for index, (_label, item_value) in enumerate(self._items):
            if item_value == value:
                self._current = value
                if self._toggles is not None:
                    if hasattr(self._toggles, "get_toggle"):
                        toggle = self._toggles.get_toggle(index)
                        if toggle is not None and hasattr(toggle, "set_enabled"):
                            # The target's exemption applies before the
                            # move: activating a disabled toggle would
                            # clear the group instead of selecting it.
                            toggle.set_enabled(True)
                    self._toggles.set_active(index)
                else:
                    assert self._combo is not None
                    self._combo.set_selected(index)
                # After the move, so the newly active toggle keeps its
                # exemption and the one left behind loses it.
                self._render_enabled()
                return
