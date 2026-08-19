"""ScanMole: GTK4/libadwaita frontend for the ``scanmole`` CLI.

A deliberately thin GUI: it builds a ``scanmole --json`` command line from the
form, streams the CLI's JSON-lines events into a persistent result bar and
offers the finished PDF. All scanning and OCR work happens in the ``scanmole``
executable (resolved from ``PATH``).

Widget labels, status texts and dialogs are translatable via gettext (see
:mod:`scanmole_gui.i18n`); the log pane stays English on purpose, because it
mixes in output of the English-only CLI.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import (  # noqa: E402  # after require_version
    Adw,
    Gdk,
    Gio,
    GLib,
    Gtk,
)

# The GUI holds no pipeline logic; the pure naming helper is imported only so
# the live filename preview matches what the CLI will produce.
from scanmole.external import run_command  # noqa: E402  # supervised capture
from scanmole.naming import DEFAULT_OUTPUT_TEMPLATE, expand_template  # noqa: E402
from scanmole.negotiation import (  # noqa: E402
    ADVISORY_PROBE_TIMEOUT_SECONDS,
    Support,
    advisory_faint_assessment,
    assess_mode,
    assess_resolution,
    assess_source,
    probe_snapshot,
)
from scanmole_gui import __version__, incompatible_cli  # noqa: E402
from scanmole_gui.i18n import _, ngettext  # noqa: E402  # after gi setup
from scanmole_gui.modes import SCAN_MODES  # noqa: E402
from scanmole_gui.probing import (  # noqa: E402
    ProbeCoordinator,
    ProbeRequest,
    selection_blocked,
)
from scanmole_gui.protocol import RawLine, decode_stdout  # noqa: E402
from scanmole_gui.request import ScanRequest, request_argv  # noqa: E402
from scanmole_gui.runner import SIGKILL_GRACE_SECONDS, ScanRunner  # noqa: E402
from scanmole_gui.session import (  # noqa: E402
    SessionState,
    Update,
    apply_event,
    complete,
    mark_cancelled,
)

LOGGER = logging.getLogger(__name__)

APP_ID = "com.foundata.ScanMole"
PROJECT_URL = "https://foundata.com/en/projects/scanmole/"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "scanmole" / "gui.json"
ICON_DIR = Path(__file__).resolve().parent / "icons"
LOGO_FILE = ICON_DIR / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"

SOURCES = (
    (_("Flatbed"), "flatbed"),
    (_("ADF"), "adf"),
    (_("ADF Duplex"), "adf-duplex"),
    (_("ADF Back"), "adf-back"),
)
SOURCE_TOOLTIPS = (
    _("Single page from the flatbed glass"),
    _("Automatic Document Feeder — front sides only"),
    _("Automatic Document Feeder — both sides of every sheet"),
    _("Automatic Document Feeder — back sides only"),
)
# Literal labels: xgettext only extracts literal _() calls, so building
# this from SCAN_MODES would silently drop the labels from the catalog.
MODES = (
    (_("B/W"), "lineart"),
    (_("Gray"), "gray"),
    (_("Color"), "color"),
    (_("B/W (faint)"), "lineart-auto"),
)
if tuple(value for _label, value in MODES) != tuple(
    value for _label, value in SCAN_MODES
):  # pragma: no cover -- import-time consistency guard
    raise RuntimeError("MODES and scanmole_gui.modes.SCAN_MODES diverged")
MODE_TOOLTIPS = (
    _("Black and white (1-bit)"),
    "",
    "",
    _(
        "Black and White (1-bit) for faint originals "
        "(e.g., thermal-paper receipts, washed-out copies)"
    ),
)
RESOLUTION_PRESETS = (200, 250, 300, 600)
RESOLUTION_MINIMUM = 50
RESOLUTION_MAXIMUM = 1200
PAGE_SIZES = (
    (_("Automatic"), "auto"),
    ("A4", "a4"),
    ("A5", "a5"),
    ("A6", "a6"),
    (_("Letter"), "letter"),
    (_("Legal"), "legal"),
)
# The tie-break family for ambiguous automatic sizes (A4 and Letter often
# both fit the content). A preference, never a restriction; the CLI value
# is carried alongside the label, so behavior never parses translations.
AUTO_SIZE_PREFERENCES = (
    (_("ISO (A sizes)"), "iso"),
    (_("North America (Letter/Legal)"), "north-american"),
)
LANGUAGES = (
    (_("German (deu)"), "deu"),
    (_("English (eng)"), "eng"),
    (_("German + English (deu+eng)"), "deu+eng"),
)
# Endonyms on purpose: a language name is most recognizable in itself.
UI_LANGUAGES = ((_("System default"), ""), ("English", "en"), ("Deutsch", "de"))
COLOR_SCHEMES = ((_("System default"), ""), (_("Light"), "light"), (_("Dark"), "dark"))

# Rough size per page at 300 dpi, from measured fleet scans; scaled by dpi².
# Content-dependent, so only ever presented as an approximation.
_SIZE_BASE_MB = {"lineart": 0.1, "lineart-auto": 0.1, "gray": 0.3, "color": 0.5}

# Friendly texts for the CLI's documented exit codes.
EXIT_HINTS: dict[int, tuple[str, str]] = {
    6: (
        _("No Pages Scanned"),
        _(
            "No pages were scanned — is the ADF (Automatic Document Feeder) "
            "loaded?\n"
            "(All pages may also have been detected as blank.)"
        ),
    ),
    3: (
        _("Scanner Error"),
        _(
            "The scanner reported an error.\n"
            "\n"
            "Make sure it is connected, powered on and not in use by another "
            "application, then try again."
        ),
    ),
    4: (
        _("Missing Dependency"),
        _(
            "scanmole is missing a required tool (e.g. scanimage, img2pdf, "
            "ocrmypdf) on this system. See the log for details."
        ),
    ),
    5: (
        _("Processing Failed"),
        _(
            "PDF assembly or OCR failed after scanning. The scanned pages "
            "were kept; see the log for the folder path."
        ),
    ),
}

DEVICE_POLL_SECONDS = 15
"""Pause between device searches while no scanner has been found.

Counted from the end of the previous search, so slow probes never shrink
the quiet gap. A probe costs a second or two of backend I/O plus discovery
traffic (sane-airscan emits mDNS/WSD queries): cheap at this cadence, so no
backoff; a suspended (minimized/hidden) window defers instead.
"""

DEFAULT_WINDOW_SIZE = (645, 840)  # starts in the single-column layout

# App-level styling: compact resolution preset chips, a dpi entry sized to
# its digits, and no separator between the .joined-below/.joined-above row
# pair (the preset row reads as the continuation of the Resolution row, not
# a new setting); both border directions covered, themes differ in which
# side they draw the hairline on.
_APP_CSS = """
button.chip { min-height: 24px; padding: 0px 8px; font-size: 0.85em; }
entry.dpi { min-width: 0px; padding-left: 8px; padding-right: 8px; }
list.boxed-list > row.joined-above { border-top: none; box-shadow: none; }
list.boxed-list > row.joined-below { border-bottom: none; box-shadow: none; }
"""


def find_scanmole() -> str:
    """Return the ``scanmole`` executable on ``PATH``, or the bare name."""
    return shutil.which("scanmole") or "scanmole"


def ensure_app_icon() -> None:
    """Copy or refresh the mascot icon in the user icon theme.

    The non-intrusive half of the desktop integration: the icon alone
    changes nothing until a desktop entry exists, but keeps an installed
    entry's icon current across package updates.
    """
    try:
        if not LOGO_FILE.is_file():
            return
        target = (
            Path(GLib.get_user_data_dir())
            / "icons"
            / "hicolor"
            / "scalable"
            / "apps"
            / f"{APP_ID}.svg"
        )
        icon_data = LOGO_FILE.read_bytes()
        if not target.is_file() or target.read_bytes() != icon_data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(icon_data)
    except OSError:
        LOGGER.debug("icon installation skipped", exc_info=True)  # a convenience


def desktop_entry_path() -> Path:
    """Return the user-level desktop entry location."""
    return Path(GLib.get_user_data_dir()) / "applications" / f"{APP_ID}.desktop"


def install_desktop_entry() -> bool:
    """Write the user-level desktop entry pinning the current executable.

    Desktop entries are a freedesktop.org standard: launchers and window
    switchers on GNOME, KDE Plasma and the other XDG desktops show an
    application's name and logo only when a desktop file matches the
    application id (environments without the concept ignore the file).
    Installing it is a deliberate action in the settings dialog, not a
    startup side effect: with uv-managed environments the executable path
    is not stable, so pinning it (and refreshing it after the environment
    moved) should be the user's call. The future RPM ships system-wide
    files instead.
    """
    try:
        ensure_app_icon()
        executable = shutil.which("scanmole-gui") or str(Path(sys.argv[0]).resolve())
        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ScanMole\n"
            "Comment=Scan documents from a SANE scanner straight to a "
            "searchable PDF\n"
            "Comment[de]=Scannt Dokumente von einem SANE-Scanner direkt in "
            "ein durchsuchbares PDF\n"
            f'Exec="{executable}"\n'
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "StartupNotify=true\n"
            "Categories=Office;Scanning;\n"
        )
        target = desktop_entry_path()
        if not target.is_file() or target.read_text(encoding="utf-8") != desktop_entry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(desktop_entry, encoding="utf-8")
        return True
    except OSError:
        return False


def remove_desktop_entry() -> bool:
    """Delete the user-level desktop entry (the icon may stay; it is inert)."""
    try:
        desktop_entry_path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


def default_folder() -> str:
    """Return the XDG Documents folder, falling back to the home directory."""
    docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    return str(docs) if docs else str(Path.home())


def abbreviate_home(path: str) -> str:
    """Render a path with the home directory abbreviated to ``~``."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def load_settings() -> dict[str, object]:
    """Load persisted GUI settings, returning an empty dict on any error."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}  # first start
    except (OSError, ValueError):
        LOGGER.debug("settings unreadable; starting fresh", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def store_settings(data: dict[str, object]) -> None:
    """Persist GUI settings; failures only cost this snapshot.

    Written to a sibling file and renamed atomically: an interrupted
    in-place write would leave invalid JSON behind, silently resetting
    every preference on the next launch.
    """
    staging = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, CONFIG_FILE)
    except OSError:
        LOGGER.debug("could not persist settings", exc_info=True)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


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


def as_int(value: object, fallback: int) -> int:
    """Return ``value`` as an int when it is one, else ``fallback``.

    JSON event fields are untrusted input; plural forms need real ints.
    """
    return value if isinstance(value, int) else fallback


class ChoiceRow:
    """A preferences row choosing one of a few fixed ``(label, value)`` items.

    Renders the options as an inline ``Adw.ToggleGroup`` (all choices visible,
    per the design) when libadwaita provides it (>= 1.7); older platforms
    (e.g. Ubuntu 24.04) fall back to a plain ``Adw.ComboRow`` so the full
    option set stays available everywhere.
    """

    def __init__(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        items: tuple[tuple[str, str], ...],
        on_change: Callable[[], None] | None = None,
        tooltips: tuple[str, ...] | None = None,
    ) -> None:
        """Build the row inside ``group``.

        ``tooltips`` explains the items one by one (empty string = none);
        the dropdown fallback has no per-item tooltips.
        """
        self._items = items
        self._on_change = on_change
        self._blocked: dict[str, str] = {}
        self._on_blocked: Callable[[str, str], None] | None = None
        self._reverting = False
        self._current = items[0][1]
        if hasattr(Adw, "ToggleGroup"):
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
        if self._toggles is not None and hasattr(self._toggles, "get_toggle"):
            for index, (_label, value) in enumerate(self._items):
                toggle = self._toggles.get_toggle(index)
                if toggle is not None and hasattr(toggle, "set_enabled"):
                    toggle.set_enabled(value not in blocked)

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
                    self._toggles.set_active(index)
                else:
                    assert self._combo is not None
                    self._combo.set_selected(index)
                return


# PyGObject has no stubs, so the GTK base class is Any; subclassing it is the
# GTK boundary that cannot be typed.
class MainWindow(Adw.ApplicationWindow):  # type: ignore[misc]
    """The single application window: scan form, log and result bar."""

    def __init__(self, **kwargs: object) -> None:
        """Build the UI, restore settings and start device discovery."""
        super().__init__(**kwargs)
        self.set_title("ScanMole")

        self._scanmole = find_scanmole()
        self._settings = load_settings()

        # Restore the remembered window geometry.
        self.set_default_size(
            as_int(self._settings.get("window_width"), DEFAULT_WINDOW_SIZE[0]),
            as_int(self._settings.get("window_height"), DEFAULT_WINDOW_SIZE[1]),
        )
        if bool(self._settings.get("window_maximized")):
            self.maximize()

        # Runtime state
        self._runner: ScanRunner | None = None
        self._session = SessionState(drop_blanks=True)
        self._closing = False
        self._close_patience = 0
        self._searching = False
        self._device_poll_id: int | None = None
        self._probes = ProbeCoordinator()
        self._base_snapshot: object = None
        self._negotiation_logged_failure = False
        self._last_caps: object = None
        self._selection_block_reason: str | None = None
        self._devices: list[dict[str, str]] = []
        self._preferred_source = "adf-duplex"
        self._reconciling_source = False
        self._run_folder = Path(default_folder())
        self._last_output: Path | None = None
        self._folder = str(self._settings.get("folder") or default_folder())
        self._languages: list[tuple[str, str]] = list(LANGUAGES)
        self._current_language = "deu+eng"
        self._cli_version: str | None = None
        # Set from the hello handshake: a truthy value blocks scanning because
        # the CLI's major version does not match this GUI (see ARCHITECTURE.md).
        self._cli_blocked = False
        self._version_alert_shown = False
        self._settings_dialog: Adw.PreferencesDialog | None = None
        # The language this process actually runs with; a differing persisted
        # value means a restart is pending.
        self._startup_ui_language = str(self._settings.get("ui_language") or "")
        self._res_syncing = False

        self._build_ui()
        self._apply_saved_settings()
        self.connect("close-request", self._on_close_request)

        self._refresh_devices()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        """Assemble the header bar, the form sections and the result bar."""
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="ScanMole"))
        # The orthodox GNOME primary menu: Settings and About live behind the
        # hamburger button instead of standalone header actions.
        menu = Gio.Menu()
        menu.append(_("Settings"), "win.settings")
        menu.append(_("About ScanMole"), "win.about")
        menu_btn = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text=_("Main menu"),
        )
        header.pack_end(menu_btn)
        for name, callback in (
            ("settings", self._on_settings_action),
            ("about", self._on_about_clicked),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        toolbar.set_content(scroller)

        self._clamp = Adw.Clamp(maximum_size=1080, tightening_threshold=900)
        scroller.set_child(self._clamp)

        container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=18,
            margin_bottom=18,
            margin_start=16,
            margin_end=16,
        )
        self._narrow_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, visible=False
        )
        # A grid (not two independent columns) so paired sections start at
        # the same height: Scanner|Output, Document|Application,
        # Processing|Log each share a grid row.
        self._grid = Gtk.Grid(
            column_spacing=24, row_spacing=18, column_homogeneous=True
        )
        container.append(self._narrow_box)
        container.append(self._grid)
        # Credit block below the form, outside the layout switching so it
        # always spans the full width; same identity layout as the About
        # dialog (logo, bold line, tagline).
        credit = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=14,
            halign=Gtk.Align.CENTER,
            margin_top=10,
        )
        if LOGO_FILE.is_file():
            credit_logo = Gtk.Image.new_from_file(str(LOGO_FILE))
            credit_logo.set_pixel_size(60)
            credit.append(credit_logo)
        credit_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2
        )
        credit_title = Gtk.Label(xalign=0.0)
        credit_title.set_markup(
            _('ScanMole %(version)s by <a href="%(url)s">foundata</a>')
            % {"version": __version__, "url": PROJECT_URL}
        )
        credit_title.add_css_class("heading")
        credit_labels.append(credit_title)
        credit_tagline = Gtk.Label(
            label=_("Easy document scanning for Linux"), xalign=0.0
        )
        credit_tagline.add_css_class("dim-label")
        credit_labels.append(credit_tagline)
        credit.append(credit_labels)
        container.append(credit)
        self._clamp.set_child(container)

        self._build_scanner_group()
        self._build_output_group()
        self._build_document_group()
        self._build_processing_group()
        self._equalize_form_rows()
        self._build_log_area()

        self._form_groups = (
            self._scanner_grp,
            self._doc_grp,
            self._proc_grp,
            self._out_grp,
        )

        toolbar.add_bottom_bar(self._build_result_bar())

        # Responsive layout: two columns only when each column can give the
        # form fields their full width (~430 px per column, matching the
        # mockup); below that, one column. The default window width starts
        # above the breakpoint.
        self._apply_layout(wide=True)
        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 920sp")
        )
        breakpoint.connect("apply", lambda *_a: self._apply_layout(wide=False))
        breakpoint.connect("unapply", lambda *_a: self._apply_layout(wide=True))
        self.add_breakpoint(breakpoint)

        self._on_document_changed()

    def _apply_layout(self, *, wide: bool) -> None:
        """Arrange the form sections in one column or a two-column grid."""
        grid_cells = (
            (self._scanner_grp, 0, 0, 1),
            (self._out_grp, 1, 0, 1),
            (self._doc_grp, 0, 1, 1),
            (self._proc_grp, 1, 1, 1),
            (self._log_area, 0, 2, 2),  # spans both columns
        )
        sections_narrow = (
            self._scanner_grp,
            self._out_grp,
            self._doc_grp,
            self._proc_grp,
            self._log_area,
        )
        for section in sections_narrow:
            parent = section.get_parent()
            if parent is not None:
                parent.remove(section)
        if wide:
            for section, column, row, width in grid_cells:
                self._grid.attach(section, column, row, width, 1)
        else:
            for section in sections_narrow:
                self._narrow_box.append(section)
        self._grid.set_visible(wide)
        self._narrow_box.set_visible(not wide)
        self._clamp.set_maximum_size(1080 if wide else 640)
        self._clamp.set_tightening_threshold(900 if wide else 480)

    def _build_scanner_group(self) -> None:
        """Build the Scanner group including the primary Scan action."""
        self._scanner_grp = Adw.PreferencesGroup(title=_("Scanner"))
        # Never make this row insensitive: the refresh button is one of its
        # suffix children, and a disabled row would take the only way to
        # recover from an empty device list down with it.
        self._device_row = Adw.ComboRow(
            title=_("Device"),
            subtitle=_("Searching for scanners…"),
        )
        self._device_row.set_factory(plain_string_factory())
        self._device_row.connect("notify::selected", self._on_device_selected)
        # The rescan action sits beside the device list, not in the header:
        # the action lives where its object is (mockup rule).
        self._refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Refresh devices"),
        )
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", self._refresh_devices)
        self._device_row.add_suffix(self._refresh_btn)
        self._scanner_grp.add(self._device_row)
        self._source_row = ChoiceRow(
            self._scanner_grp,
            _("Source"),
            SOURCES,
            self._on_source_changed,
            tooltips=SOURCE_TOOLTIPS,
        )

        # One primary action: Scan is the only accented control, full width at
        # the bottom of the Scanner group (mockup rule); Cancel swaps in while
        # a scan runs. The buttons are wrapped in list rows because a plain
        # widget given to PreferencesGroup.add() lands below the card, not in
        # it.
        self._scan_btn = Gtk.Button(
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8
        )
        self._scan_btn.set_child(
            Adw.ButtonContent(
                icon_name="media-playback-start-symbolic", label=_("Scan")
            )
        )
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.add_css_class("pill")
        self._scan_btn.connect("clicked", self._on_scan_clicked)
        self._scan_row = Gtk.ListBoxRow(
            child=self._scan_btn, activatable=False, selectable=False
        )
        self._scanner_grp.add(self._scan_row)
        self._cancel_btn = Gtk.Button(
            label=_("Cancel"),
            margin_top=8,
            margin_bottom=8,
            margin_start=8,
            margin_end=8,
        )
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.add_css_class("pill")
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        self._cancel_row = Gtk.ListBoxRow(
            child=self._cancel_btn, activatable=False, selectable=False, visible=False
        )
        self._scanner_grp.add(self._cancel_row)

    def _build_output_group(self) -> None:
        """Build the Output group (folder, filename template)."""
        self._out_grp = Adw.PreferencesGroup(title=_("Output"))
        self._folder_row = Adw.ActionRow(title=_("Folder"))
        self._folder_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )
        self._folder_btn.connect("clicked", self._on_pick_folder)
        self._folder_row.add_suffix(self._folder_btn)
        self._folder_row.set_activatable_widget(self._folder_btn)
        self._out_grp.add(self._folder_row)

        # Deliberately about twice a default row: entry, placeholder helper
        # and preview stack so the Output group lines up with the Scanner
        # group's Device/Source/Scan rows in the two-column grid. A custom
        # row (not Adw.ActionRow) so the title can align with the entry at
        # the top instead of centering over the whole stack.
        name_row_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=17,
            margin_start=12,
            margin_end=12,
        )
        name_title = Gtk.Label(
            label=_("File name"),
            xalign=0.0,
            valign=Gtk.Align.START,
            margin_top=18,
        )
        name_row_box.append(name_title)
        name_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            valign=Gtk.Align.CENTER,
            hexpand=True,
            margin_top=10,
            margin_bottom=10,
        )
        self._name_entry = Gtk.Entry(
            placeholder_text=DEFAULT_OUTPUT_TEMPLATE, width_chars=34, hexpand=True
        )
        self._name_entry.connect("changed", self._update_name_preview)
        # Focusing the empty field materializes the default template so it
        # can be edited instead of retyped; leaving it unchanged still counts
        # as "use the default" (the save logic normalizes it back).
        name_focus = Gtk.EventControllerFocus()
        name_focus.connect("enter", self._on_name_entry_focus)
        self._name_entry.add_controller(name_focus)
        name_box.append(self._name_entry)
        hint = Gtk.Label(
            label=_(
                "Placeholders: {YYYY} {MM} {DD} {hh} {mm} {ss} {device}\n"
                "{N} (auto-no., 0-padded, repeatable)"
            ),
            xalign=1.0,
            wrap=True,
            justify=Gtk.Justification.RIGHT,
            max_width_chars=44,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        name_box.append(hint)
        self._name_preview = Gtk.Label(xalign=1.0)
        self._name_preview.add_css_class("caption")
        self._name_preview.add_css_class("dim-label")
        self._name_preview.set_ellipsize(3)  # Pango.EllipsizeMode.END
        name_box.append(self._name_preview)
        name_row_box.append(name_box)
        self._name_row = Gtk.ListBoxRow(
            child=name_row_box, activatable=False, selectable=False
        )
        self._out_grp.add(self._name_row)

    def _build_document_group(self) -> None:
        """Build the Document group (color mode, page size, resolution)."""
        self._doc_grp = Adw.PreferencesGroup(title=_("Document"))
        self._mode_row = ChoiceRow(
            self._doc_grp,
            _("Color mode"),
            MODES,
            self._on_document_changed,
            tooltips=MODE_TOOLTIPS,
        )
        # Page size plus the automatic-size family preference in one row:
        # the second dropdown only matters (and is only sensitive) while
        # the first says Automatic; it keeps its value while disabled.
        self._size_row = Adw.ActionRow(title=_("Page size"))
        size_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        self._size_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _value in PAGE_SIZES])
        )
        self._size_dropdown.set_factory(plain_string_factory())
        self._size_dropdown.connect("notify::selected", self._on_page_size_changed)
        size_box.append(self._size_dropdown)
        self._size_pref_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new([label for label, _value in AUTO_SIZE_PREFERENCES])
        )
        self._size_pref_dropdown.set_factory(plain_string_factory())
        self._size_pref_dropdown.set_tooltip_text(
            _("Resolves ambiguous automatic page sizes (A4 and Letter often both fit).")
        )
        self._size_pref_dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Automatic size preference")]
        )
        self._size_pref_dropdown.connect("notify::selected", self._on_document_changed)
        size_box.append(self._size_pref_dropdown)
        self._size_row.add_suffix(size_box)
        self._doc_grp.add(self._size_row)

        # Hybrid resolution control, composed as entry / unit / stepper so
        # the unit sits between the number and the buttons (GtkSpinButton
        # cannot render a unit suffix). The static bounds are a sanity clamp
        # only; the CLI snaps the value to what the device actually supports
        # during capability negotiation.
        self._res_row = Adw.ActionRow(title=_("Resolution"))
        entry_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        self._res_value = 300
        self._res_entry = Gtk.Entry(
            text="300",
            width_chars=4,
            max_width_chars=4,
            max_length=4,
            xalign=1.0,
            input_purpose=Gtk.InputPurpose.DIGITS,
        )
        self._res_entry.add_css_class("dpi")
        self._res_entry.connect("activate", self._on_resolution_commit)
        res_focus = Gtk.EventControllerFocus()
        res_focus.connect("leave", self._on_resolution_commit)
        self._res_entry.add_controller(res_focus)
        res_keys = Gtk.EventControllerKey()
        res_keys.connect("key-pressed", self._on_resolution_key)
        self._res_entry.add_controller(res_keys)
        res_scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        res_scroll.connect("scroll", self._on_resolution_scroll)
        self._res_entry.add_controller(res_scroll)
        entry_box.append(self._res_entry)
        dpi_label = Gtk.Label(label="dpi")
        dpi_label.add_css_class("dim-label")
        entry_box.append(dpi_label)
        stepper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        stepper.add_css_class("linked")
        minus_btn = Gtk.Button(icon_name="list-remove-symbolic")
        minus_btn.connect("clicked", lambda *_a: self._step_resolution(-10))
        stepper.append(minus_btn)
        plus_btn = Gtk.Button(icon_name="list-add-symbolic")
        plus_btn.connect("clicked", lambda *_a: self._step_resolution(10))
        stepper.append(plus_btn)
        entry_box.append(stepper)
        self._res_row.add_suffix(entry_box)
        self._res_row.add_css_class("joined-below")
        self._doc_grp.add(self._res_row)

        # The dpi presets get their own row directly below Resolution: the
        # split gives Document the same four-row shape as Processing, which
        # _equalize_form_rows() then locks to identical heights.
        self._chips_row = Adw.ActionRow()
        self._chips_row.add_css_class("joined-above")
        chips = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            halign=Gtk.Align.END,
            valign=Gtk.Align.CENTER,
        )
        chips.add_css_class("linked")
        self._res_chips: list[tuple[Gtk.ToggleButton, int]] = []
        for preset in RESOLUTION_PRESETS:
            chip = Gtk.ToggleButton(label=str(preset))
            chip.add_css_class("chip")
            chip.connect("toggled", self._on_resolution_chip, preset)
            chips.append(chip)
            self._res_chips.append((chip, preset))
        self._chips_row.add_suffix(chips)
        self._doc_grp.add(self._chips_row)

    def _build_processing_group(self) -> None:
        """Build the Processing group (blank pages, OCR, language)."""
        self._proc_grp = Adw.PreferencesGroup(title=_("Processing"))
        self._blank_row = Adw.SwitchRow(
            title=_("Skip blank pages"),
            subtitle=_("Removes pages detected as empty"),
            active=True,
        )
        self._proc_grp.add(self._blank_row)
        self._ocr_row = Adw.SwitchRow(
            title=_("OCR"),
            subtitle=_("Make the PDF text-searchable (PDF/A)"),
            active=True,
        )
        self._ocr_row.connect("notify::active", self._on_ocr_toggled)
        self._proc_grp.add(self._ocr_row)
        self._lang_row = Adw.ComboRow(title=_("OCR Language"))
        # Same 20-character default-factory cap as the device row: without a
        # plain-label factory, "German + English (deu+eng)" gets ellipsized.
        self._lang_row.set_factory(plain_string_factory())
        self._set_language_model()
        self._lang_row.connect("notify::selected", self._on_language_selected)
        self._proc_grp.add(self._lang_row)
        self._deskew_row = Adw.SwitchRow(
            title=_("Deskew"),
            subtitle=_("Correct skewed scanned pages"),
            active=True,
        )
        self._proc_grp.add(self._deskew_row)

    def _equalize_form_rows(self) -> None:
        """Lock Document and Processing to the same height, row by row.

        Both cards have four rows; a vertical size group per cross-column
        pair makes the sections end flush, with the resolution entry and
        preset rows together exactly as tall as OCR language plus deskew.
        The groups must outlive this method (widgets do not reference them).
        """
        self._row_size_groups: list[Gtk.SizeGroup] = []
        for left, right in (
            (self._mode_row.row, self._blank_row),
            (self._size_row, self._ocr_row),
            (self._res_row, self._lang_row),
            (self._chips_row, self._deskew_row),
        ):
            size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)
            size_group.add_widget(left)
            size_group.add_widget(right)
            self._row_size_groups.append(size_group)

    def _build_log_area(self) -> None:
        """Build the collapsed, copyable log below the form."""
        self._log_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._log_expander = Gtk.Expander(
            label=_("Log"), hexpand=True, valign=Gtk.Align.CENTER
        )
        log_header.append(self._log_expander)
        copy_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        copy_btn.set_child(
            Adw.ButtonContent(icon_name="edit-copy-symbolic", label=_("Copy"))
        )
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", self._on_copy_log)
        log_header.append(copy_btn)
        self._log_area.append(log_header)
        log_scroller = Gtk.ScrolledWindow(
            min_content_height=210, has_frame=True, visible=False
        )
        self._log_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            left_margin=6,
            right_margin=6,
            top_margin=4,
            bottom_margin=4,
        )
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_buf = self._log_view.get_buffer()
        self._log_end = self._log_buf.create_mark(
            None, self._log_buf.get_end_iter(), False
        )
        log_scroller.set_child(self._log_view)
        self._log_expander.connect(
            "notify::expanded",
            lambda *_a: log_scroller.set_visible(self._log_expander.get_expanded()),
        )
        self._log_area.append(log_scroller)

    def _build_result_bar(self) -> Gtk.Box:
        """Build the persistent bottom bar showing progress and the result."""
        # Centered as a whole: with mixed icon, two-line text and buttons a
        # left-aligned bar never lines up optically with the groups above.
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self._status_spinner = Gtk.Spinner(visible=False)
        bar.append(self._status_spinner)
        self._status_icon = Gtk.Image(icon_name="object-select-symbolic", visible=False)
        bar.append(self._status_icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self._status_title = Gtk.Label(xalign=0.0, label=_("Ready."))
        self._status_title.add_css_class("heading")
        self._status_title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        labels.append(self._status_title)
        self._status_detail = Gtk.Label(xalign=0.0, visible=False)
        self._status_detail.add_css_class("caption")
        self._status_detail.add_css_class("dim-label")
        self._status_detail.add_css_class("monospace")
        self._status_detail.set_ellipsize(3)
        labels.append(self._status_detail)
        bar.append(labels)
        self._show_btn = Gtk.Button(visible=False)
        self._show_btn.set_child(
            Adw.ButtonContent(icon_name="folder-open-symbolic", label=_("Show"))
        )
        self._show_btn.connect("clicked", lambda *_a: self._show_in_folder())
        bar.append(self._show_btn)
        self._open_btn = Gtk.Button(visible=False)
        self._open_btn.set_child(
            Adw.ButtonContent(icon_name="x-office-document-symbolic", label=_("Open"))
        )
        self._open_btn.connect("clicked", lambda *_a: self._open_output())
        bar.append(self._open_btn)
        return bar

    def _set_result_bar(
        self,
        state: str,
        title: str,
        detail: str = "",
    ) -> None:
        """Put the bottom bar into ``idle``/``running``/``success``/``error``."""
        self._status_title.set_text(title)
        self._status_detail.set_text(detail)
        self._status_detail.set_visible(bool(detail))
        running = state == "running"
        self._status_spinner.set_visible(running)
        if running:
            self._status_spinner.start()
        else:
            self._status_spinner.stop()
        self._status_icon.set_visible(state in ("success", "error"))
        self._status_icon.set_from_icon_name(
            "dialog-error-symbolic" if state == "error" else "object-select-symbolic"
        )
        if state == "success":
            self._status_icon.add_css_class("success")
        else:
            self._status_icon.remove_css_class("success")
        finished = state == "success" and self._last_output is not None
        self._show_btn.set_visible(finished)
        self._open_btn.set_visible(finished)

    # ------------------------------------------------------- settings I/O

    def _apply_saved_settings(self) -> None:
        """Restore form widgets from the persisted settings."""
        settings = self._settings
        self._preferred_source = str(settings.get("source", "adf-duplex"))
        self._source_row.select(self._preferred_source)
        self._mode_row.select(str(settings.get("mode", "lineart")))
        try:
            resolution = int(str(settings.get("resolution", "300")))
        except ValueError:
            resolution = 300
        self._set_resolution(resolution)
        combo_select(
            self._size_dropdown, PAGE_SIZES, str(settings.get("page_size", "auto"))
        )
        # A missing or unknown saved value keeps the default (index 0: ISO).
        combo_select(
            self._size_pref_dropdown,
            AUTO_SIZE_PREFERENCES,
            str(settings.get("auto_size_preference", "iso")),
        )
        self._on_page_size_changed()
        self._ocr_row.set_active(bool(settings.get("ocr", True)))
        self._deskew_row.set_active(bool(settings.get("deskew", True)))
        self._select_language(str(settings.get("lang", "deu+eng")))
        self._lang_row.set_sensitive(self._ocr_row.get_active())
        self._blank_row.set_active(bool(settings.get("skip_blanks", True)))
        template = str(settings.get("filename_template") or "")
        self._name_entry.set_text(
            "" if template in ("", DEFAULT_OUTPUT_TEMPLATE) else template
        )
        self._apply_color_scheme(str(settings.get("color_scheme") or ""))
        self._folder_btn.set_child(
            Adw.ButtonContent(
                icon_name="folder-symbolic", label=abbreviate_home(self._folder)
            )
        )
        self._on_document_changed()

    def _set_language_model(self) -> None:
        """Rebuild the language dropdown: known languages plus "Add more…"."""
        labels = [label for label, _value in self._languages]
        labels.append(_("Add more…"))
        self._lang_row.set_model(Gtk.StringList.new(labels))

    def _select_language(self, lang: str) -> None:
        """Select ``lang`` in the language list, adding a custom entry if new."""
        if lang and lang not in [value for _label, value in self._languages]:
            self._languages.append((lang, lang))
            self._set_language_model()
        for index, (_label, value) in enumerate(self._languages):
            if value == lang:
                self._lang_row.set_selected(index)
                self._current_language = lang
                return

    def _on_language_selected(self, *_args: object) -> None:
        """Track real selections; "Add more…" opens the language help."""
        index = int(self._lang_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._languages):
            self._current_language = self._languages[index][1]
            return
        # The "Add more…" pseudo item: revert to the previous selection and
        # explain how additional Tesseract languages are managed.
        self._select_language(self._current_language)
        self._on_more_languages()

    def _on_more_languages(self) -> None:
        """Explain OCR language management and take a custom code to use."""
        dialog = Adw.AlertDialog(
            heading=_("More OCR Languages"),
            body=_(
                "OCR languages are Tesseract language packs, installed and "
                "removed with the distribution's package manager. The codes "
                "are three-letter ISO 639-2/T codes (deu, eng, fra, ...).\n"
                "\n"
                "Fedora: sudo dnf install tesseract-langpack-fra\n"
                "Debian/Ubuntu: sudo apt install tesseract-ocr-fra\n"
                "(remove instead of install to uninstall)\n"
                "\n"
                "To use installed languages here, enter the codes below; "
                "combine several with +."
            ),
        )
        # Roughly double the default alert width so the install commands fit
        # on one line; the extra child's minimum width backs the request up.
        dialog.set_content_width(680)
        entry = Gtk.Entry(placeholder_text="deu+fra")
        entry.set_size_request(440, -1)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("use", _("Use language"))
        dialog.set_response_appearance("use", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("use")
        dialog.set_close_response("cancel")

        def on_response(_dialog: object, response: str) -> None:
            code = entry.get_text().strip()
            if response == "use" and code:
                self._select_language(code)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _save_settings(self) -> None:
        """Snapshot the current form into the settings file.

        Merges over the loaded settings so keys written elsewhere (window
        geometry) survive the snapshot.
        """
        self._settings = {
            **self._settings,
            "device": self._selected_device() or "",
            # The user's own choice, not a temporary sole-source adoption:
            # a duplex-capable scanner must get the preference back.
            "source": self._preferred_source,
            "mode": self._mode_row.value(),
            "resolution": str(self._current_resolution()),
            "page_size": combo_value(self._size_dropdown, PAGE_SIZES),
            "auto_size_preference": combo_value(
                self._size_pref_dropdown, AUTO_SIZE_PREFERENCES
            ),
            "ocr": self._ocr_row.get_active(),
            "deskew": self._deskew_row.get_active(),
            "lang": self._selected_language(),
            "skip_blanks": self._blank_row.get_active(),
            "filename_template": self._current_template(),
            "folder": self._folder,
        }
        store_settings(self._settings)

    # ----------------------------------------------------------- devices

    def _refresh_devices(self, *_args: object) -> None:
        """Start an asynchronous ``scanmole --list-devices`` in a worker thread."""
        if self._runner is not None or self._searching:
            return
        self._searching = True
        self._refresh_btn.set_sensitive(False)
        self._device_row.set_subtitle(_("Searching for scanners…"))
        prefer = self._selected_device() or str(self._settings.get("device") or "")
        threading.Thread(
            target=self._devices_worker, args=(prefer,), daemon=True
        ).start()

    def _devices_worker(self, prefer: str) -> None:
        """Worker thread: query devices and hand results back to the main loop."""
        if self._cli_version is None:
            self._cli_version = self._probe_cli_version()
        devices: list[dict[str, str]] = []
        err = ""
        try:
            # The supervised engine helper: the query runs in its own
            # process group, so a wedged backend probe cannot leave
            # descendants behind on timeout.
            result = run_command(
                [self._scanmole, "--list-devices", "--json"],
                timeout_seconds=120,
            )
            hello_version: str | None = None
            for raw in result.stdout.splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue  # valid JSON, wrong shape: not ours to crash on
                if event.get("event") == "hello":
                    hello_version = str(event.get("version") or "") or None
                if event.get("event") == "devices":
                    # Hide virtual devices (webcams, SANE test backend); skip
                    # entries that are not objects instead of crashing.
                    devices = [
                        device
                        for device in event.get("devices") or []
                        if isinstance(device, dict)
                        and not str(device.get("device", "")).startswith(
                            ("v4l:", "test:")
                        )
                    ]
            if result.stderr.strip():
                GLib.idle_add(self._append_log, result.stderr.strip())
            if hello_version is not None:
                self._cli_version = hello_version
            # Refuse a CLI whose major does not match instead of guessing: a
            # mismatched protocol rendering silently wrong state is worse
            # than a hard stop. A run without hello predates the handshake.
            needed = (
                incompatible_cli(__version__, hello_version)
                if hello_version is not None or result.returncode == 0
                else None
            )
            self._cli_blocked = needed is not None
            if needed is not None:
                devices = []
                err = _(
                    "Incompatible scanmole CLI: found version %(found)s, "
                    "but this GUI needs %(needed)s."
                ) % {"found": hello_version or _("unknown"), "needed": needed}
            elif result.returncode != 0 and not devices:
                err = _("Device search failed (exit %(code)d).") % {
                    "code": result.returncode
                }
        except FileNotFoundError:
            err = _("scanmole CLI not found — install it or add it to PATH.")
        except subprocess.TimeoutExpired:
            err = _("Device search timed out.")
        except OSError as exc:
            err = _("Device search failed: %(error)s") % {"error": exc}
        except Exception:  # defensive: the UI must never stay dead
            LOGGER.debug("device search failed unexpectedly", exc_info=True)
            err = _("Device search failed unexpectedly.")
        finally:
            # Always reaches the main loop, or the refresh button and the
            # "Searching for scanners…" subtitle would stay stuck forever.
            GLib.idle_add(self._apply_devices, devices, err, prefer)

    def _probe_cli_version(self) -> str | None:
        """Return the supervised CLI's version string, or ``None``."""
        try:
            result = run_command([self._scanmole, "--version"], timeout_seconds=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = result.stdout.strip().splitlines()
        parts = lines[0].split() if lines else []
        return parts[-1] if len(parts) >= 2 else None

    def _apply_devices(
        self, devices: list[dict[str, str]], err: str, prefer: str
    ) -> None:
        """Populate the device dropdown on the main loop."""
        self._searching = False
        self._refresh_btn.set_sensitive(self._runner is None)
        self._scan_btn.set_sensitive(
            self._runner is None
            and not self._cli_blocked
            and self._selection_block_reason is None
        )
        if self._cli_blocked and err and not self._version_alert_shown:
            self._version_alert_shown = True
            self._alert(_("Incompatible scanmole CLI"), err)
        self._devices = devices
        names = []
        for device in devices:
            vendor = (device.get("vendor") or "").strip()
            model = (device.get("model") or "").strip()
            names.append(
                " ".join(p for p in (vendor, model) if p)
                or device.get("device", _("Unknown device"))
            )
        self._device_row.set_model(Gtk.StringList.new(names))
        # The row itself must stay sensitive either way: disabling it would
        # also disable its refresh-button suffix, leaving no way to rescan.
        if devices:
            index = next(
                (i for i, d in enumerate(devices) if d.get("device") == prefer), 0
            )
            self._device_row.set_selected(index)
            self._device_row.set_subtitle("")
            self._device_row.set_tooltip_text(devices[index].get("device", ""))
            self._set_result_bar(
                "idle",
                ngettext("Found %d scanner.", "Found %d scanners.", len(devices))
                % len(devices),
            )
            self._start_negotiation()
        else:
            self._device_row.set_tooltip_text("")
            self._device_row.set_subtitle(
                err or _("No scanners found — connect one and press Refresh.")
            )
            self._set_result_bar("idle", _("No scanners found."))
            if err:
                self._append_log(f"[gui] {err}")
        # Plug-in-after-start flow: keep looking on our own while nothing was
        # found, stop the moment something is (issue #7). One-shot chain, so
        # the pause counts from the end of a search, not its start; every
        # completed search (auto or manual refresh) restarts the countdown.
        if self._device_poll_id is not None:
            GLib.source_remove(self._device_poll_id)
            self._device_poll_id = None
        if not devices and not self._cli_blocked:
            self._device_poll_id = GLib.timeout_add_seconds(
                DEVICE_POLL_SECONDS, self._poll_devices
            )

    def _poll_devices(self) -> bool:
        """One automatic re-search attempt; only while the list is empty."""
        self._device_poll_id = None
        if self._devices or self._cli_blocked:
            return bool(GLib.SOURCE_REMOVE)
        suspended = self.is_suspended() if hasattr(self, "is_suspended") else False
        if self._runner is not None or self._searching or suspended:
            # Transient: defer a full interval; the next completed search
            # would reschedule anyway, this covers scans and hidden windows.
            self._device_poll_id = GLib.timeout_add_seconds(
                DEVICE_POLL_SECONDS, self._poll_devices
            )
            return bool(GLib.SOURCE_REMOVE)
        self._append_log("[gui] no scanner yet — searching again")
        self._refresh_devices()
        return bool(GLib.SOURCE_REMOVE)  # the search result schedules the next

    def _selected_device(self) -> str | None:
        """Return the SANE id of the selected device, or ``None``."""
        index = int(self._device_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._devices):
            return self._devices[index].get("device")
        return None

    def _on_device_selected(self, *_args: object) -> None:
        """Expose the selected device's SANE id as a tooltip.

        As a tooltip (not a subtitle) the diagnostic id costs no row width,
        so the selected model name renders without ellipses.
        """
        self._device_row.set_tooltip_text(self._selected_device() or "")
        self._update_name_preview()
        self._start_negotiation()

    # -------------------------------------------- capability negotiation

    def _on_source_changed(self) -> None:
        """The source choice changed: refine mode-dependent options."""
        if not self._reconciling_source:
            # A manual change states a preference; a programmatic
            # reconciliation (sole-source adoption, preference restore)
            # must not overwrite what the user actually wants.
            self._preferred_source = self._source_row.value()
        self._update_selection_block()
        device = self._selected_device()
        caps = self._base_snapshot
        if device is None or self._runner is not None or not isinstance(caps, dict):
            return
        assessment = assess_source(caps, self._source_row.value())
        if assessment.backend_value is not None:
            self._launch_probe(
                ProbeRequest(device, (("--source", assessment.backend_value),))
            )

    def _start_negotiation(self) -> None:
        """Kick off an advisory capability probe for the selected device.

        Two stages: a bare probe derives source availability, a follow-up
        with the negotiated source applied refines the mode-dependent
        options. Serialized through the coordinator; stale results are
        dropped by generation token. Never probes while a scan owns the
        device. Advisory only: the engine re-negotiates before every scan.
        """
        device = self._selected_device()
        if device is None or self._runner is not None:
            return
        self._launch_probe(ProbeRequest(device))

    def _launch_probe(self, request: ProbeRequest) -> None:
        hit, snapshot = self._probes.cached(request)
        if hit:
            self._apply_snapshot(request, snapshot)
            return
        token = self._probes.begin(request)
        if token is None:
            return  # queued behind the running probe
        threading.Thread(
            target=self._probe_worker, args=(token, request), daemon=True
        ).start()

    def _probe_worker(self, token: int, request: ProbeRequest) -> None:
        snapshot = probe_snapshot(
            request.device, request.settings, ADVISORY_PROBE_TIMEOUT_SECONDS
        )
        GLib.idle_add(self._on_probe_done, token, request, snapshot)

    def _on_probe_done(
        self, token: int, request: ProbeRequest, snapshot: object
    ) -> None:
        current, follow_up = self._probes.complete(token, snapshot)
        if follow_up is not None:
            self._launch_probe(follow_up)
        if not current or request.device != self._selected_device():
            return  # stale: the user moved on
        self._apply_snapshot(request, snapshot)

    def _apply_snapshot(self, request: ProbeRequest, snapshot: object) -> None:
        caps = snapshot if isinstance(snapshot, dict) else None
        if caps is None and not self._negotiation_logged_failure:
            self._negotiation_logged_failure = True
            self._append_log(
                "[gui] capability probe failed; leaving all options selectable"
            )
        if not request.settings:
            # Bare snapshot: source availability, then refine the modes with
            # the currently selected source applied.
            self._base_snapshot = caps
            blocked: dict[str, str] = {}
            for value in ("flatbed", "adf", "adf-duplex", "adf-back"):
                assessment = assess_source(caps, value)
                if selection_blocked(assessment.support):
                    blocked[value] = assessment.consequence
            self._source_row.set_availability(blocked, self._on_choice_blocked)
            self._reconcile_source_choice(blocked)
            selected = assess_source(caps, self._source_row.value())
            if caps is not None and selected.backend_value is not None:
                self._launch_probe(
                    ProbeRequest(
                        request.device,
                        (("--source", selected.backend_value),),
                    )
                )
        self._last_caps = caps
        self._apply_mode_availability(caps)
        self._on_document_changed()
        self._update_selection_block()

    def _reconcile_source_choice(self, blocked: dict[str, str]) -> None:
        """Re-apply the user's source preference to new availability.

        The preferred source wins whenever the device offers it. When it
        is blocked and exactly one source remains selectable (the ScanSnap
        iX100 offers ADF Front alone), that sole source is adopted so
        Start stays usable instead of demanding a pointless click; the
        stored preference is untouched, so a capable scanner gets it back.
        While a real choice remains, nothing is changed silently: Start
        stays disabled with the reason, exactly as before.
        """
        available = [value for _label, value in SOURCES if value not in blocked]
        target: str | None = None
        if self._preferred_source in available:
            target = self._preferred_source
        elif self._source_row.value() in blocked and len(available) == 1:
            target = available[0]
            self._append_log(
                f"[gui] '{target}' is the only source this scanner offers; selected it"
            )
        if target is not None and target != self._source_row.value():
            self._reconciling_source = True
            try:
                self._source_row.select(target)
            finally:
                self._reconciling_source = False

    def _apply_mode_availability(self, caps: object) -> None:
        capabilities = caps if isinstance(caps, dict) else None
        blocked: dict[str, str] = {}
        for value in ("lineart", "gray", "color", "lineart-auto"):
            # The faint mode takes the optimistic advisory verdict: a
            # visible native-enhancement signature keeps it selectable, and
            # the engine confirms the path with staged probes at scan time.
            assessment = (
                advisory_faint_assessment(capabilities)
                if value == "lineart-auto"
                else assess_mode(capabilities, value)
            )
            if selection_blocked(assessment.support):
                blocked[value] = assessment.consequence
        self._mode_row.set_availability(blocked, self._on_choice_blocked)

    def _on_choice_blocked(self, value: str, reason: str) -> None:
        """A visible-but-unavailable choice was clicked: explain, keep state."""
        self._set_result_bar("idle", _("Not available on this scanner: %s") % reason)

    def _update_selection_block(self) -> None:
        """Disable Start while the active saved choice is unavailable.

        The selection is never changed silently; the user must pick another
        value themselves.
        """
        reason = self._source_row.blocked_reason() or self._mode_row.blocked_reason()
        self._selection_block_reason = reason
        self._scan_btn.set_sensitive(
            self._runner is None and not self._cli_blocked and reason is None
        )
        if reason is not None:
            self._set_result_bar(
                "idle", _("Selected option not available: %s") % reason
            )

    # ------------------------------------------------- live consequences

    def _selected_language(self) -> str:
        """Return the Tesseract language code(s) of the selected item."""
        index = int(self._lang_row.get_selected())  # untyped GTK call
        if 0 <= index < len(self._languages):
            return self._languages[index][1]
        return self._current_language  # "Add more…" is never a language

    def _current_template(self) -> str:
        """Return the filename template from the form, with .pdf ensured."""
        template = self._name_entry.get_text().strip() or DEFAULT_OUTPUT_TEMPLATE
        if not template.lower().endswith(".pdf"):
            template += ".pdf"
        return template

    def _current_resolution(self) -> int:
        """Return the dpi from the hybrid resolution control."""
        return self._res_value

    def _set_resolution(self, value: int) -> None:
        """Clamp and apply a dpi value to the entry, chips and previews."""
        value = max(RESOLUTION_MINIMUM, min(RESOLUTION_MAXIMUM, value))
        self._res_value = value
        if self._res_entry.get_text() != str(value):
            self._res_entry.set_text(str(value))
            self._res_entry.set_position(-1)
        self._sync_resolution_chips()
        self._on_document_changed()

    def _on_resolution_commit(self, *_args: object) -> None:
        """Validate the typed dpi on Enter or focus-out; revert if invalid."""
        text = self._res_entry.get_text().strip()
        try:
            self._set_resolution(int(text))
        except ValueError:
            self._set_resolution(self._res_value)

    def _step_resolution(self, delta: int) -> None:
        """Step the dpi by ``delta`` from the stepper, keys or scroll wheel."""
        self._on_resolution_commit()  # honor a value typed but not committed
        self._set_resolution(self._res_value + delta)

    def _on_resolution_key(
        self, _controller: object, keyval: int, _keycode: int, _state: object
    ) -> bool:
        """Handle Up/Down arrows in the dpi entry."""
        if keyval == Gdk.KEY_Up:
            self._step_resolution(10)
            return True
        if keyval == Gdk.KEY_Down:
            self._step_resolution(-10)
            return True
        return False

    def _on_resolution_scroll(self, _controller: object, _dx: float, dy: float) -> bool:
        """Handle scroll-wheel stepping over the dpi entry."""
        self._step_resolution(-10 if dy > 0 else 10)
        return True

    def _sync_resolution_chips(self) -> None:
        """Highlight the preset chip matching the entry, or none."""
        self._res_syncing = True
        current = self._current_resolution()
        for chip, preset in self._res_chips:
            chip.set_active(preset == current)
        self._res_syncing = False

    def _on_resolution_chip(self, chip: Gtk.ToggleButton, preset: int) -> None:
        """Fill the entry from a clicked preset chip."""
        if self._res_syncing:
            return
        if chip.get_active():
            self._set_resolution(preset)
        else:
            # Clicking the active chip again keeps it active; the "no chip"
            # state is reached by typing a custom value, not by unselecting.
            self._sync_resolution_chips()

    def _on_page_size_changed(self, *_args: object) -> None:
        """Gate the family preference: it only applies in automatic mode."""
        automatic = combo_value(self._size_dropdown, PAGE_SIZES) == "auto"
        self._size_pref_dropdown.set_sensitive(automatic)
        self._on_document_changed()

    def _on_document_changed(self, *_args: object) -> None:
        """Refresh the size estimate and the filename preview."""
        dpi = self._current_resolution()
        base = _SIZE_BASE_MB.get(self._mode_row.value(), 0.3)
        estimate = max(base * (dpi / 300.0) ** 2, 0.1)
        capabilities = self._last_caps if isinstance(self._last_caps, dict) else None
        assessment = assess_resolution(capabilities, dpi)
        effective = (
            int(assessment.effective)
            if assessment.support is Support.DEGRADED
            else None
        )
        if effective is not None and effective != dpi:
            self._res_row.set_subtitle(
                _(
                    "%(dpi)d dpi · scans at %(effective)d dpi · "
                    "approx. %(size).1f MB per page"
                )
                % {
                    "dpi": dpi,
                    "effective": effective,
                    "size": max(base * (effective / 300.0) ** 2, 0.1),
                }
            )
            self._update_name_preview()
            return
        self._res_row.set_subtitle(
            _("%(dpi)d dpi · approx. %(size).1f MB per page")
            % {"dpi": dpi, "size": estimate}
        )
        self._update_name_preview()

    def _on_name_entry_focus(self, *_args: object) -> None:
        """Prefill the default template on focus so it can be edited."""
        if not self._name_entry.get_text():
            self._name_entry.set_text(DEFAULT_OUTPUT_TEMPLATE)
            self._name_entry.set_position(-1)

    def _update_name_preview(self, *_args: object) -> None:
        """Render the template with the current form values as an example."""
        example = expand_template(
            self._current_template(),
            when=datetime.now().astimezone(),
            counter=1,
            device=self._selected_device() or "device",
        )
        self._name_preview.set_text(_("Preview: %(name)s") % {"name": example})

    # ------------------------------------------------------- application

    def _apply_color_scheme(self, value: str) -> None:
        """Apply a color scheme value (``""``/``light``/``dark``) globally."""
        schemes = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        Adw.StyleManager.get_default().set_color_scheme(
            schemes.get(value, Adw.ColorScheme.DEFAULT)
        )

    def _store_pref(self, key: str, value: str) -> None:
        """Persist one settings-dialog preference immediately."""
        self._settings[key] = value
        store_settings(self._settings)

    def _on_settings_action(self, *_args: object) -> None:
        """Open the settings dialog (color scheme, language, reset)."""
        dialog = Adw.PreferencesDialog(title=_("Settings"))
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        scheme_row = Adw.ComboRow(title=_("Color scheme"))
        scheme_row.set_model(
            Gtk.StringList.new([label for label, _value in COLOR_SCHEMES])
        )
        combo_select(
            scheme_row, COLOR_SCHEMES, str(self._settings.get("color_scheme") or "")
        )

        def scheme_changed(*_a: object) -> None:
            value = combo_value(scheme_row, COLOR_SCHEMES)
            self._apply_color_scheme(value)
            self._store_pref("color_scheme", value)

        scheme_row.connect("notify::selected", scheme_changed)
        group.add(scheme_row)

        lang_row = Adw.ComboRow(
            title=_("Interface language"), subtitle=_("Restart required")
        )
        lang_row.set_model(
            Gtk.StringList.new([label for label, _value in UI_LANGUAGES])
        )
        combo_select(
            lang_row, UI_LANGUAGES, str(self._settings.get("ui_language") or "")
        )
        group.add(lang_row)

        desktop_row = Adw.ActionRow(
            title=_("Desktop entry"),
            subtitle=_("Show ScanMole in the application launcher and window switcher"),
        )
        installed = desktop_entry_path().is_file()
        remove_btn = Gtk.Button(label=_("Remove"), valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("destructive-action")
        remove_btn.set_sensitive(installed)
        desktop_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        desktop_btn.set_label(_("Update") if installed else _("Install"))

        def install_clicked(*_a: object) -> None:
            if install_desktop_entry():
                desktop_btn.set_label(_("Update"))
                remove_btn.set_sensitive(True)
                dialog.add_toast(Adw.Toast(title=_("Desktop entry installed.")))
            else:
                dialog.add_toast(
                    Adw.Toast(title=_("Could not install the desktop entry."))
                )

        def remove_clicked(*_a: object) -> None:
            if remove_desktop_entry():
                desktop_btn.set_label(_("Install"))
                remove_btn.set_sensitive(False)
                dialog.add_toast(Adw.Toast(title=_("Desktop entry removed.")))
            else:
                dialog.add_toast(
                    Adw.Toast(title=_("Could not remove the desktop entry."))
                )

        desktop_btn.connect("clicked", install_clicked)
        remove_btn.connect("clicked", remove_clicked)
        desktop_row.add_suffix(remove_btn)
        desktop_row.add_suffix(desktop_btn)
        desktop_row.set_activatable_widget(desktop_btn)
        group.add(desktop_row)

        reset_row = Adw.ActionRow(
            title=_("Reset settings"),
            subtitle=_("Restore all options to their defaults"),
        )
        reset_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        reset_btn.set_child(
            Adw.ButtonContent(
                icon_name="edit-undo-symbolic", label=_("Reset to defaults")
            )
        )
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self._on_reset_clicked)
        reset_row.add_suffix(reset_btn)
        reset_row.set_activatable_widget(reset_btn)
        group.add(reset_row)

        # Inactive until a change actually needs a restart (the interface
        # language differs from what this process started with).
        restart_row = Adw.ActionRow(
            title=_("Restart now"),
            subtitle=_("Apply changes that require a restart"),
        )
        restart_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        restart_btn.set_child(
            Adw.ButtonContent(icon_name="view-refresh-symbolic", label=_("Restart"))
        )
        restart_btn.add_css_class("suggested-action")
        restart_btn.connect("clicked", self._on_restart_clicked)
        restart_row.add_suffix(restart_btn)
        restart_row.set_activatable_widget(restart_btn)
        group.add(restart_row)

        def update_restart_row() -> None:
            pending = (
                str(self._settings.get("ui_language") or "")
                != self._startup_ui_language
            )
            restart_row.set_sensitive(pending)

        def language_changed(*_a: object) -> None:
            self._store_pref("ui_language", combo_value(lang_row, UI_LANGUAGES))
            update_restart_row()

        lang_row.connect("notify::selected", language_changed)
        update_restart_row()

        page.add(group)
        dialog.add(page)
        self._settings_dialog = dialog
        dialog.connect("closed", lambda *_a: setattr(self, "_settings_dialog", None))
        dialog.present(self)

    def _on_restart_clicked(self, *_args: object) -> None:
        """Quit and re-execute the application (see ``main``)."""
        application = self.get_application()
        if application is not None:
            application.restart_requested = True
        dialog = self._settings_dialog
        if dialog is not None:
            # An open dialog swallows the window's close(); close the dialog
            # first and chain the window close onto its closed signal.
            dialog.connect("closed", lambda *_a: self.close())
            dialog.close()
        else:
            self.close()

    def _on_reset_clicked(self, *_args: object) -> None:
        """Ask for confirmation, then reset the GUI settings to defaults."""
        dialog = Adw.AlertDialog(
            heading=_("Reset Settings?"),
            body=_(
                "All GUI options return to their defaults. Scanned files and "
                "folders on disk are not touched."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset"))
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_reset_response)
        dialog.present(self)

    def _on_reset_response(self, _dialog: object, response: str) -> None:
        """Apply the settings reset when confirmed."""
        if response != "reset":
            return
        # Only the GUI settings file is cleared; scans, output folders and
        # the CLI are never touched.
        self._settings = {}
        store_settings(self._settings)
        self._folder = default_folder()
        self._apply_saved_settings()
        # Also resize back to the default geometry; without this the close
        # handler would immediately re-persist the current size and the reset
        # would never reach the window.
        if self.is_maximized():
            self.unmaximize()
        self.set_default_size(*DEFAULT_WINDOW_SIZE)
        # The open settings dialog still shows the pre-reset selections;
        # close it, the next open rebuilds from the defaults.
        if self._settings_dialog is not None:
            self._settings_dialog.close()
        self._set_result_bar("idle", _("Settings reset to defaults."))

    def _on_about_clicked(self, *_args: object) -> None:
        """Show a flat, single-page About dialog (no nested subpages)."""
        dialog = Adw.Dialog(title=_("About ScanMole"), content_width=440)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=12,
            margin_bottom=20,
            margin_start=20,
            margin_end=20,
        )

        identity = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        if LOGO_FILE.is_file():
            logo = Gtk.Image.new_from_file(str(LOGO_FILE))
            logo.set_pixel_size(72)
            identity.append(logo)
        id_labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2
        )
        name = Gtk.Label(label="ScanMole", xalign=0.0)
        name.add_css_class("title-4")
        id_labels.append(name)
        tagline = Gtk.Label(label=_("Easy document scanning for Linux"), xalign=0.0)
        tagline.add_css_class("dim-label")
        id_labels.append(tagline)
        identity.append(id_labels)
        content.append(identity)

        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for key, value in (
            ("scanmole CLI", self._cli_version or _("unknown")),
            ("scanmole-gui", __version__),
            (_("License"), "GPL-3.0-or-later"),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            key_label = Gtk.Label(label=key, xalign=0.0, hexpand=True)
            row.append(key_label)
            value_label = Gtk.Label(label=value, xalign=1.0)
            value_label.add_css_class("monospace")
            row.append(value_label)
            facts.append(row)
        content.append(facts)

        description = Gtk.Label(
            label=_(
                "ScanMole scans documents through SANE, detects blank pages, "
                "assembles PDFs, and optionally makes them text-searchable "
                "with OCR."
            ),
            xalign=0.0,
            wrap=True,
        )
        description.add_css_class("dim-label")
        content.append(description)

        website = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        website_label = Gtk.Label(label=_("Website:"), valign=Gtk.Align.CENTER)
        website.append(website_label)
        link = Gtk.LinkButton.new_with_label(
            PROJECT_URL, "foundata.com/en/projects/scanmole"
        )
        link.set_halign(Gtk.Align.START)
        website.append(link)
        content.append(website)

        toolbar.set_content(content)
        dialog.set_child(toolbar)
        dialog.present(self)

    # ----------------------------------------------------------- scanning

    def _current_request(self, folder: Path) -> ScanRequest:
        """Snapshot the form into an immutable scan request."""
        return ScanRequest(
            device=self._selected_device(),
            source=self._source_row.value(),
            mode=self._mode_row.value(),
            resolution=self._current_resolution(),
            page_size=combo_value(self._size_dropdown, PAGE_SIZES),
            auto_size_preference=(
                "north-american"
                if combo_value(self._size_pref_dropdown, AUTO_SIZE_PREFERENCES)
                == "north-american"
                else "iso"
            ),
            ocr=bool(self._ocr_row.get_active()),
            lang=self._selected_language(),
            deskew=bool(self._deskew_row.get_active()),
            drop_blanks=bool(self._blank_row.get_active()),
            # The CLI expands the filename placeholders and picks the next
            # free counter value; the GUI only forwards the template.
            output=str(folder / self._current_template()),
        )

    def _on_scan_clicked(self, *_args: object) -> None:
        """Validate the output folder and launch the scan subprocess."""
        if self._runner is not None:
            return
        folder = Path(self._folder).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            return
        self._save_settings()

        request = self._current_request(folder)
        self._session = SessionState(drop_blanks=request.drop_blanks)
        self._run_folder = folder
        self._last_output = None

        argv = request_argv(request, self._scanmole)
        self._append_log("$ " + shlex.join(argv))
        runner = ScanRunner(
            schedule=self._schedule,
            timer=self._after_seconds,
            on_stdout=self._on_stdout_line,
            on_stderr=self._on_stderr_line,
            on_exit=self._on_process_exit,
            on_escalated=self._on_kill_escalated,
        )
        try:
            runner.start(argv, folder)
        except OSError as exc:
            self._append_log(f"[gui] failed to start scanmole: {exc}")
            self._alert(
                _("Could Not Start scanmole"),
                f"{exc}\n\n" + _("Install the scanmole CLI somewhere in PATH."),
            )
            return
        self._runner = runner
        self._set_result_bar("running", _("Starting scanmole\u2026"))
        self._set_running(True)

    # GLib marshalling for the GTK-free runner: line and exit callbacks land
    # on the main loop as one-shot idle sources (a None return removes them),
    # and the escalation delay becomes a one-shot timeout.

    @staticmethod
    def _schedule(callback: Callable[[], None]) -> None:
        GLib.idle_add(callback)

    @staticmethod
    def _after_seconds(seconds: float, callback: Callable[[], None]) -> None:
        def fire() -> bool:
            callback()
            return bool(GLib.SOURCE_REMOVE)

        GLib.timeout_add_seconds(round(seconds), fire)

    # -------------------------------------------------- JSON event stream

    def _on_stdout_line(self, runner: ScanRunner, line: str) -> None:
        """Fold one stdout line into the session and render the change."""
        if runner is not self._runner:
            return  # stale run
        decoded = decode_stdout(line)
        if decoded is None:
            return
        if isinstance(decoded, RawLine):
            self._append_log(decoded.text)  # non-event stdout: just log it
            return
        self._session, update = apply_event(self._session, decoded)
        self._render_update(update)

    def _render_update(self, update: Update) -> None:
        """Render a session update into translated result-bar text."""
        state = self._session
        if update is Update.STARTED:
            self._set_result_bar("running", _("Scanning\u2026"))
        elif update is Update.PAGE:
            text = _("Page %d scanned") % state.pages
            if state.blanks:
                text += (
                    ngettext(
                        " (%d blank skipped)", " (%d blanks skipped)", state.blanks
                    )
                    % state.blanks
                )
            self._set_result_bar("running", text + "\u2026")
        elif update is Update.SCAN_DONE:
            total = state.total or 0
            kept = state.kept or 0
            self._set_result_bar(
                "running",
                ngettext(
                    "Scan finished \u2014 keeping %(kept)d of %(total)d page\u2026",
                    "Scan finished \u2014 keeping %(kept)d of %(total)d pages\u2026",
                    total,
                )
                % {"kept": kept, "total": total},
            )
        elif update is Update.OCR_STARTED:
            self._set_result_bar("running", _("Running OCR\u2026"))
        elif update is Update.ERROR:
            message = state.error_message or _("Unknown error")
            self._append_log(f"[error] {message}")

    def _on_stderr_line(self, runner: ScanRunner, line: str) -> None:
        """Append a raw stderr line to the log view."""
        if runner is not self._runner:
            return  # stale run: same identity guard as stdout and exit
        line = line.rstrip("\n")
        if line:
            self._append_log(line)

    # ------------------------------------------------------- process exit

    def _on_process_exit(self, runner: ScanRunner, exit_code: int) -> None:
        """Finalize the UI when the scan subprocess exits."""
        if runner is not self._runner:
            return
        self._runner = None
        self._set_running(False)
        self._append_log(f"[gui] scanmole exited with code {exit_code}")

        outcome = complete(self._session, exit_code, self._run_folder)
        if outcome.kind == "cancelled":
            self._set_result_bar("idle", _("Scan cancelled."))
            return
        if outcome.kind == "success":
            if outcome.output is not None:
                self._last_output = outcome.output
                summary = (
                    ngettext("%d page saved", "%d pages saved", outcome.pages)
                    % outcome.pages
                )
                if outcome.blanks:
                    summary += " \u00b7 " + (
                        ngettext(
                            "%d blank skipped", "%d blanks skipped", outcome.blanks
                        )
                        % outcome.blanks
                    )
                self._set_result_bar("success", summary, outcome.output.name)
            else:
                self._set_result_bar("idle", _("Finished."))
            return
        heading, body = EXIT_HINTS.get(
            outcome.exit_code,
            (
                _("Scan Failed"),
                _("scanmole exited with status %(code)d. See the log for details.")
                % {"code": outcome.exit_code},
            ),
        )
        if outcome.error_message:
            body = body + "\n\n" + _("Details:") + " " + outcome.error_message
        self._set_result_bar("error", heading)
        self._alert(heading, body)

    # ------------------------------------------------------------- cancel

    def _on_cancel_clicked(self, *_args: object) -> None:
        """Terminate the scan's process group, escalating to SIGKILL."""
        runner = self._runner
        if runner is None or not runner.cancel():
            return  # no run, already finished, or already cancelling
        self._session = mark_cancelled(self._session)
        self._cancel_btn.set_sensitive(False)
        self._set_result_bar("running", _("Cancelling\u2026"))
        self._append_log("[gui] cancelling \u2014 SIGTERM to process group")

    def _on_kill_escalated(self, runner: ScanRunner) -> None:
        """The grace period ran out: the runner is about to SIGKILL."""
        if runner is self._runner:
            self._append_log("[gui] still running \u2014 SIGKILL to process group")

    def _persist_ui_state(self) -> None:
        """Snapshot the form and window geometry to the settings file.

        The form is snapshotted here as well as at scan start, so changed
        values (mode, resolution, page size, ...) survive a restart even
        when no scan ran in between.
        """
        self._settings["window_maximized"] = bool(self.is_maximized())
        if not self.is_maximized():
            self._settings["window_width"] = int(self.get_width())
            self._settings["window_height"] = int(self.get_height())
        self._save_settings()

    def _shutdown_now(self) -> None:
        """Application shutdown: persist state, stop any scan synchronously.

        The main loop is ending, so GLib sources scheduled from here on
        (the cancel path's KILL escalation and exit polling) may never
        fire. The runner's synchronous barrier TERMs, KILLs and reaps the
        scan's process group on this thread instead.
        """
        self._persist_ui_state()
        runner = self._runner
        if runner is not None:
            runner.shutdown()

    def _on_close_request(self, *_args: object) -> bool:
        """Persist the form and window geometry, stop any running scan."""
        self._persist_ui_state()
        runner = self._runner
        if runner is not None and runner.is_running():
            # Closing must not orphan the engine mid-batch: run the normal
            # cancel escalation and keep the (hidden) window alive until the
            # child exited, so its cleanup finishes and nothing keeps
            # scanning invisibly.
            if not self._closing:
                self._closing = True
                self._close_patience = SIGKILL_GRACE_SECONDS + 5
                self._on_cancel_clicked()
                self.set_visible(False)
                GLib.timeout_add_seconds(1, self._destroy_when_exited, runner)
            return True  # inhibit; the poll below closes for real
        return False  # allow the window to close

    def _destroy_when_exited(self, runner: ScanRunner) -> bool:
        """Destroy the hidden window once the cancelled child is gone."""
        self._close_patience -= 1
        if runner.is_running() and self._close_patience > 0:
            return bool(GLib.SOURCE_CONTINUE)
        if runner.is_running():  # pragma: no cover -- SIGKILL failed somehow
            LOGGER.debug("closing despite a surviving child process group")
        self.destroy()
        return bool(GLib.SOURCE_REMOVE)

    # -------------------------------------------------------- UI plumbing

    def _set_running(self, running: bool) -> None:
        """Toggle the form and the primary action for a running scan."""
        self._scan_row.set_visible(not running)
        self._cancel_row.set_visible(running)
        self._cancel_btn.set_sensitive(True)
        self._refresh_btn.set_sensitive(not running)
        for group in self._form_groups:
            group.set_sensitive(not running)
        # The Scanner group hosts the Scan/Cancel buttons; keep them usable
        # while the rest of the group is locked during a run.
        if running:
            self._scanner_grp.set_sensitive(True)
            self._device_row.set_sensitive(False)
            self._source_row.row.set_sensitive(False)
        else:
            self._device_row.set_sensitive(True)
            self._source_row.row.set_sensitive(True)

    def _append_log(self, text: str) -> None:
        """Append a line to the log view and scroll it into view."""
        self._log_buf.insert(self._log_buf.get_end_iter(), text.rstrip("\n") + "\n")
        self._log_view.scroll_to_mark(self._log_end, 0.0, False, 0.0, 1.0)

    def _on_copy_log(self, *_args: object) -> None:
        """Copy the whole log text to the clipboard."""
        start, end = self._log_buf.get_bounds()
        text = self._log_buf.get_text(start, end, True)
        provider = Gdk.ContentProvider.new_for_value(text)
        self.get_clipboard().set_content(provider)

    def _alert(self, heading: str, body: str) -> None:
        """Present a simple modal alert dialog."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def _on_ocr_toggled(self, *_args: object) -> None:
        """Enable the language selection only while OCR is on."""
        self._lang_row.set_sensitive(self._ocr_row.get_active())

    def _on_pick_folder(self, *_args: object) -> None:
        """Open a folder chooser for the output directory."""
        dialog = Gtk.FileDialog(title=_("Choose Output Folder"), modal=True)
        if self._folder and Path(self._folder).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(self._folder))
        dialog.select_folder(self, None, self._on_folder_picked)

    def _on_folder_picked(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        """Store the chosen output folder, ignoring a dismissed dialog."""
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self._folder = gfile.get_path()
            self._folder_btn.set_child(
                Adw.ButtonContent(
                    icon_name="folder-symbolic", label=abbreviate_home(self._folder)
                )
            )

    def _open_output(self) -> None:
        """Open the produced PDF in the default application."""
        if self._last_output is None:
            return
        uri = Gio.File.new_for_path(str(self._last_output)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            subprocess.Popen(
                ["xdg-open", str(self._last_output)], start_new_session=True
            )

    def _show_in_folder(self) -> None:
        """Reveal the produced PDF in the file manager."""
        if self._last_output is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self._last_output)))
        launcher.open_containing_folder(self, None, None)


# Same GTK boundary as MainWindow: the base class is Any without stubs.
class ScanMoleApp(Adw.Application):  # type: ignore[misc]
    """The libadwaita application owning a single :class:`MainWindow`."""

    def __init__(self) -> None:
        """Register the activation handler."""
        super().__init__(application_id=APP_ID)
        # Set by the settings dialog's Restart row; main() re-executes the
        # process after the main loop ends.
        self.restart_requested: bool = False
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _on_shutdown(self, *_args: object) -> None:
        """Persist state and stop any scan when the application quits.

        Ctrl+C (PyGObject's SIGINT fallback calls ``quit()``) ends the main
        loop directly, bypassing the window's ``close-request`` handler, and
        GLib sources scheduled from here on may never fire, so the window's
        timer-based close escalation cannot be trusted anymore. Delegate to
        the synchronous shutdown barrier while the window is still alive;
        on the normal close path the window is already gone here.
        """
        window = self.props.active_window
        shutdown_now = getattr(window, "_shutdown_now", None)
        if shutdown_now is not None:
            shutdown_now()

    def _on_activate(self, app: Adw.Application) -> None:
        """Present the main window, creating it on first activation."""
        ensure_app_icon()
        # Make the packaged mascot icon resolvable by name (About dialog).
        display = Gdk.Display.get_default()
        if display is not None:
            if ICON_DIR.is_dir():
                Gtk.IconTheme.get_for_display(display).add_search_path(str(ICON_DIR))
            provider = Gtk.CssProvider()
            provider.load_from_string(_APP_CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        win = self.props.active_window or MainWindow(application=app)
        win.present()


def main(argv: list[str] | None = None) -> int:
    """Run the ScanMole GUI and return the application exit code."""
    GLib.set_application_name("ScanMole")
    app = ScanMoleApp()
    try:
        code = int(app.run(sys.argv if argv is None else argv))  # untyped GTK call
    except KeyboardInterrupt:
        # PyGObject's SIGINT fallback quits the main loop cleanly, then
        # re-raises so the caller learns about the interrupt; map it to the
        # conventional exit code instead of a traceback.
        return 130
    if app.restart_requested:
        # Re-execute the process so the launcher re-applies the persisted
        # interface language before gettext binds. The stale LANGUAGE from
        # this process must not leak into the replacement.
        from scanmole_gui import preferred_ui_language

        language = preferred_ui_language()
        if language:
            os.environ["LANGUAGE"] = language
        else:
            os.environ.pop("LANGUAGE", None)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
