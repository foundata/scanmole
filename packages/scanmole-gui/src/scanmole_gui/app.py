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

import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
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
from scanmole.config import SheetFlow  # noqa: E402  # a pure type alias
from scanmole.negotiation import (  # noqa: E402
    Support,
    assess_resolution,
)
from scanmole_gui import __version__, desktop  # noqa: E402
from scanmole_gui.deviceflow import (  # noqa: E402
    DeviceContext,
    DeviceFlow,
    DiscoveryFailure,
    ListingOutcome,
    SensorTrigger,
)
from scanmole_gui.dialogs import (  # noqa: E402
    build_about_dialog,
    build_more_languages_dialog,
    build_settings_dialog,
)
from scanmole_gui.discovery import display_name  # noqa: E402
from scanmole_gui.form import (  # noqa: E402
    JBIG2_HINT,
    ScanForm,
    abbreviate_home,
    default_folder,
    hardware_button_value,
)
from scanmole_gui.i18n import _, ngettext  # noqa: E402  # after gi setup
from scanmole_gui.previewflow import PreviewFlow, PreviewInputs  # noqa: E402
from scanmole_gui.probing import CapabilityUpdate  # noqa: E402
from scanmole_gui.protocol import RawLine, decode_stdout  # noqa: E402
from scanmole_gui.request import request_argv  # noqa: E402
from scanmole_gui.runner import SIGKILL_GRACE_SECONDS, ScanRunner  # noqa: E402
from scanmole_gui.session import (  # noqa: E402
    SessionState,
    Update,
    apply_event,
    complete,
    mark_cancelled,
)
from scanmole_gui.settings import (  # noqa: E402
    load_settings,
    reset_settings,
    store_settings,
)
from scanmole_gui.status import (  # noqa: E402
    LogView,
    ResultBar,
    close_confirmation_text,
    exit_failure_texts,
    render_session_update,
    success_summary,
)

LOGGER = logging.getLogger(__name__)

APP_ID = "com.foundata.ScanMole"
PROJECT_URL = "https://foundata.com/en/projects/scanmole/"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "scanmole" / "gui.json"
ICON_DIR = Path(__file__).resolve().parent / "icons"
LOGO_FILE = ICON_DIR / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"

# Above the layout breakpoint and tall enough for both columns, so a first
# start shows every setting instead of hiding some below the fold. A screen
# too small for it gets a window clamped to the work area by the compositor.
DEFAULT_WINDOW_SIZE = (1015, 800)

# App-level styling: compact resolution preset chips, a dpi entry sized to
# its digits, and no separator between the .joined-below/.joined-above row
# pair (the preset row reads as the continuation of the Resolution row, not
# a new setting); both border directions covered, themes differ in which
# side they draw the hairline on. The presets get a little air above them
# on top of that, which both loosens the pair and lets the Scan card meet
# the other column's height. Its own class, because .joined-above is on
# the output group's hint row too and padding both would cancel out. The
# log's Copy button matches the weight of the expander beside it; that
# rule has to name the label node, because the theme puts the bold there
# rather than on the button the label would inherit it from.
_APP_CSS = """
button.chip { min-height: 24px; padding: 0px 8px; font-size: 0.85em; }
entry.dpi { min-width: 0px; padding-left: 8px; padding-right: 8px; }
list.boxed-list > row.joined-above { border-top: none; box-shadow: none; }
list.boxed-list > row.joined-below { border-bottom: none; box-shadow: none; }
list.boxed-list > row.presets { padding-top: 6px; }
button.log-copy label { font-weight: normal; }
"""


def find_scanmole() -> str:
    """Return the ``scanmole`` executable this GUI should drive.

    ``PATH`` decides where it answers, so an explicitly placed engine
    still wins. Where it does not, the console script installed beside
    this GUI or beside the interpreter running it does: a desktop
    launcher passes the session's environment rather than the shell's,
    so a GUI installed into a virtual environment sees no ``PATH`` entry
    for the engine sitting right next to it. That fallback only ever
    acts where the search would otherwise have come up empty.
    """
    found = shutil.which("scanmole")
    if found is not None:
        return found
    neighbours = []
    if os.sep in sys.argv[0]:
        # A launcher starts the GUI by absolute path; resolving a bare
        # name would only point into the working directory.
        neighbours.append(Path(sys.argv[0]).resolve().parent)
    neighbours.append(Path(sys.executable).parent)
    for directory in neighbours:
        candidate = directory / "scanmole"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "scanmole"


# Thin XDG adapters: GLib knows the platform directories, the GTK-free
# desktop module owns the entry text and the file lifecycle.


def app_icon_target() -> Path:
    """The user icon-theme location of the application icon."""
    return (
        Path(GLib.get_user_data_dir())
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / f"{APP_ID}.svg"
    )


def ensure_app_icon() -> None:
    """Copy or refresh the mascot icon in the user icon theme."""
    desktop.ensure_icon(LOGO_FILE, app_icon_target())


def desktop_entry_path() -> Path:
    """Return the user-level desktop entry location."""
    return Path(GLib.get_user_data_dir()) / "applications" / f"{APP_ID}.desktop"


def install_desktop_entry() -> bool:
    """Write the user-level desktop entry pinning the current executable.

    Desktop entries are a freedesktop.org standard: launchers and window
    switchers on GNOME, KDE Plasma and the other XDG desktops show an
    application's name and logo only when a desktop file matches the
    application id (environments without the concept ignore the file).
    """
    executable = shutil.which("scanmole-gui") or str(Path(sys.argv[0]).resolve())
    return desktop.install_desktop_entry(
        desktop_entry_path(), executable, APP_ID, LOGO_FILE, app_icon_target()
    )


def remove_desktop_entry() -> bool:
    """Delete the user-level desktop entry (the icon may stay; it is inert)."""
    return desktop.remove_desktop_entry(desktop_entry_path())


def as_int(value: object, fallback: int) -> int:
    """Return ``value`` as an int when it is one, else ``fallback``.

    JSON event fields are untrusted input; plural forms need real ints.
    """
    return value if isinstance(value, int) else fallback


def restored_window_size(settings: dict[str, object]) -> tuple[int, int]:
    """The window size to restore: the persisted one when sane, else default.

    Settings files written by a persist that raced the window's
    destruction carry zeros; a nonpositive size must restore the default
    instead of a zero-sized window.
    """
    width = as_int(settings.get("window_width"), DEFAULT_WINDOW_SIZE[0])
    height = as_int(settings.get("window_height"), DEFAULT_WINDOW_SIZE[1])
    if width <= 0 or height <= 0:
        return DEFAULT_WINDOW_SIZE
    return width, height


# PyGObject has no stubs, so the GTK base class is Any; subclassing it is the
# GTK boundary that cannot be typed.
class MainWindow(Adw.ApplicationWindow):  # type: ignore[misc]
    """The single application window: scan form, log and result bar."""

    def __init__(self, **kwargs: object) -> None:
        """Build the UI, restore settings and start device discovery."""
        super().__init__(**kwargs)
        self.set_title("ScanMole")

        self._scanmole = find_scanmole()
        self._settings = load_settings(CONFIG_FILE)

        # Restore the remembered window geometry.
        self.set_default_size(*restored_window_size(self._settings))
        if bool(self._settings.get("window_maximized")):
            self.maximize()

        # Runtime state
        self._runner: ScanRunner | None = None
        self._session = SessionState(drop_blanks=True)
        self._closing = False
        self._close_confirmed = False
        """Set once a discard prompt was answered with "Close Anyway", so
        the close it triggers is not questioned a second time."""
        self._echo_log = False
        """Whether log lines also go to stderr (a confirmed discard)."""
        self._restart_pending = False
        """Set by the settings dialog's Restart row. It stays here until
        the window actually closes, because the close it asks for can
        still be refused; only then does the application learn of it."""
        self._close_patience = 0
        # Once released (window close, application shutdown), late
        # results must no longer touch the widgets.
        self._released = False
        # Asynchronous device coordination (discovery, capability probes,
        # idle sensor polling) lives in its own lifecycle owner; the
        # window renders its typed outcomes and keeps every decision that
        # needs a widget, a translation or the Start predicate.
        self._deviceflow = DeviceFlow(
            scanmole=self._scanmole,
            context=self._device_context,
            on_searching=self._render_search_started,
            on_listing=self._render_device_listing,
            on_capabilities=self._render_capability_update,
            on_trigger=self._on_sensor_trigger,
            on_log=self._append_log,
        )
        # The filename preview: a debounced, generation-tagged look at the
        # output folder plus the monitor that notices someone else writing
        # into it. Local filesystem work, deliberately separate from the
        # advisory commands that own scanner access. The window decides
        # when to ask and what a look is about; the flow owns the rest.
        self._preview = PreviewFlow(
            alive=self._preview_alive,
            inputs=self._preview_inputs,
            render=lambda text: self._form.set_preview(text),
        )
        self._selection_block_reason: str | None = None
        # The rendered device list: what each dropdown row stands for.
        # The controller keeps its own canonical copy for its unchanged
        # and vanished decisions.
        self._devices: list[dict[str, str]] = []
        self._run_folder = Path(default_folder())
        self._last_output: Path | None = None
        self._version_alert_shown = False
        self._settings_dialog: Adw.PreferencesDialog | None = None
        # The language this process actually runs with; a differing persisted
        # value means a restart is pending.
        self._startup_ui_language = str(self._settings.get("ui_language") or "")

        self._build_ui()
        self._apply_saved_settings()
        self.connect("close-request", self._on_close_request)
        self._watch_view_state()

        self._deviceflow.start()

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
            spacing=18,
            margin_top=18,
            margin_bottom=18,
            margin_start=16,
            margin_end=16,
        )
        self._narrow_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, visible=False
        )
        # Two independent columns, not a grid: the cards have very
        # different heights (Scan carries the device, the document
        # settings and the action), so each column packs its own stack
        # from the top instead of leaving a hole beside the tallest card.
        self._columns = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=24, homogeneous=True
        )
        self._left_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.START
        )
        self._right_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.START
        )
        self._columns.append(self._left_column)
        self._columns.append(self._right_column)
        container.append(self._narrow_box)
        container.append(self._columns)
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

        self._form = ScanForm(
            on_device_selected=self._on_device_selected,
            on_source_changed=self._on_source_changed,
            on_refresh=self._refresh_devices,
            on_scan=self._on_scan_clicked,
            on_cancel=self._on_cancel_clicked,
            on_pick_folder=self._on_pick_folder,
            on_more_languages=self._on_more_languages,
            on_choice_blocked=self._on_choice_blocked,
            on_hardware_button_selected=self._on_hardware_button_selected,
            on_insert_to_scan=self._on_insert_to_scan_toggled,
            on_open_settings=self._on_settings_action,
            on_preview_stale=self._preview.request,
            device_for_preview=self._selected_device,
            effective_resolution=self._effective_resolution,
        )
        self._log = LogView()
        # The log spans the full width below both columns: its monospace
        # CLI output reads badly in a narrow column, and staying outside
        # the column layout means it never moves between the two modes.
        container.insert_child_after(self._log.widget, self._columns)
        self._status = ResultBar(
            on_show=self._show_in_folder,
            on_open=self._open_output,
            on_next_sheet=self._on_next_sheet,
            on_finish=self._on_finish_collect,
        )

        toolbar.add_bottom_bar(self._status.widget)

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

        self._form.refresh_document_hints()

    def _apply_layout(self, *, wide: bool) -> None:
        """Arrange the form sections in one column or two.

        Both arrangements keep the same reading order, most-changed
        settings first: Scan, Output, Processing, Advanced. The wide
        layout splits that order after the Scan card, which is the tall
        one and now carries the whole of a single scan; everything that
        happens to the result afterwards goes beside it.
        """
        columns = (
            (self._left_column, (self._form.scan_group,)),
            (
                self._right_column,
                (
                    self._form.output_group,
                    self._form.processing_group,
                    self._form.advanced_group,
                ),
            ),
        )
        sections_narrow = (
            self._form.scan_group,
            self._form.output_group,
            self._form.processing_group,
            self._form.advanced_group,
        )
        for section in sections_narrow:
            parent = section.get_parent()
            if parent is not None:
                parent.remove(section)
        if wide:
            for column, sections in columns:
                for section in sections:
                    column.append(section)
        else:
            for section in sections_narrow:
                self._narrow_box.append(section)
        self._columns.set_visible(wide)
        self._narrow_box.set_visible(not wide)
        self._clamp.set_maximum_size(1080 if wide else 640)
        self._clamp.set_tightening_threshold(900 if wide else 480)

    def _set_result_bar(
        self, state: str, title: str, detail: str = "", note: str = ""
    ) -> None:
        """Put the bottom bar into ``idle``/``running``/``success``/``error``.

        The Show/Open actions appear only when a finished output exists,
        which only the window knows.
        """
        self._status.set_state(
            state,
            title,
            detail,
            actions=state == "success" and self._last_output is not None,
            note=note,
        )

    def _append_log(self, text: str) -> None:
        """Append a line to the log pane, and to stderr while closing.

        A confirmed discard hides the window but keeps the engine alive
        until it finished preserving; the recovery command it prints then
        would otherwise land in a log pane nobody can read again.
        """
        self._log.append(text)
        if self._echo_log:
            print(text, file=sys.stderr, flush=True)

    # ------------------------------------------------------- settings I/O

    def _apply_saved_settings(self) -> None:
        """Restore the form and application look from the persisted settings."""
        self._deviceflow.preferred_source = str(
            self._settings.get("source", "adf-duplex")
        )
        self._form.apply_settings(self._settings)
        self._apply_color_scheme(str(self._settings.get("color_scheme") or ""))

    def _on_more_languages(self) -> None:
        """Explain OCR language management and take a custom code to use."""
        build_more_languages_dialog(self._form.select_language).present(self)

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
            "source": self._deviceflow.preferred_source,
            **self._form.persisted_values(),
        }
        store_settings(CONFIG_FILE, self._settings)

    # ----------------------------------------------------------- devices

    def _device_context(self) -> DeviceContext:
        """One fresh snapshot of the window state device work reads.

        Visibility and suspension are answered separately: an ordinarily
        hidden window reports ``get_visible() == False`` without being
        compositor-suspended, and ``is_suspended`` (GTK 4.12+; older
        runtimes never report it) can hold while the widget still counts
        as visible. Neither answer is derived from the other.
        """
        return DeviceContext(
            selected_device=self._selected_device(),
            remembered_device=str(self._settings.get("device") or ""),
            source=self._form.source_value(),
            sensor_prefs=self._sensor_prefs(),
            start_allowed=self._scan_allowed(),
            visible=bool(self.get_visible()),
            suspended=self._window_suspended(),
        )

    def _refresh_devices(self, *_args: object) -> None:
        """The Refresh action: ask the controller for a new search."""
        self._deviceflow.refresh()

    def _render_search_started(self) -> None:
        """Paint the state of a non-quiet search that just started."""
        self._form.set_refresh_enabled(False)
        # No Start during a search: the CLI would only repeat the same
        # discovery and fail without a scanner.
        self._update_scan_enabled()
        self._form.set_device_subtitle(_("Searching for scanners…"))

    def _discovery_failure_text(self, outcome: ListingOutcome) -> str:
        """The localized message for one typed discovery failure."""
        if outcome.failure is None:
            return ""
        if outcome.failure is DiscoveryFailure.INCOMPATIBLE_CLI:
            return _(
                "Incompatible scanmole CLI: found version %(found)s, "
                "but this GUI needs %(needed)s."
            ) % {
                "found": outcome.cli_version or _("unknown"),
                "needed": outcome.needed,
            }
        if outcome.failure is DiscoveryFailure.FAILED_EXIT:
            return _("Device search failed (exit %(code)d).") % {
                "code": outcome.failed_exit
            }
        if outcome.failure is DiscoveryFailure.CLI_MISSING:
            return _("scanmole CLI not found — install it or add it to PATH.")
        if outcome.failure is DiscoveryFailure.TIMED_OUT:
            return _("Device search timed out.")
        if outcome.failure is DiscoveryFailure.OS_ERROR:
            return _("Device search failed: %(error)s") % {
                "error": outcome.error_detail
            }
        return _("Device search failed unexpectedly.")

    def _render_device_listing(self, outcome: ListingOutcome) -> None:
        """Populate the device dropdown from one decided search result.

        An ``unchanged`` quiet result applies nothing: no model rebuild
        (which would fire ``notify::selected`` and renegotiate), no
        subtitle, no result-bar repaint. The routine presence check
        therefore costs no visible churn.
        """
        self._form.set_refresh_enabled(self._runner is None)
        if not outcome.unchanged:
            err = self._discovery_failure_text(outcome)
            if (
                outcome.failure is DiscoveryFailure.INCOMPATIBLE_CLI
                and not self._version_alert_shown
            ):
                self._version_alert_shown = True
                self._alert(_("Incompatible scanmole CLI"), err)
            devices = outcome.devices
            self._devices = devices
            names = [display_name(device, _("Unknown device")) for device in devices]
            prefer = outcome.prefer
            # The row itself must stay sensitive either way: disabling it
            # would also disable its refresh-button suffix, leaving no way
            # to rescan.
            if devices:
                index = next(
                    (i for i, d in enumerate(devices) if d.get("device") == prefer), 0
                )
                self._form.show_devices(names, index)
                self._form.set_device_subtitle("")
                self._form.set_device_tooltip(devices[index].get("device", ""))
                self._set_result_bar(
                    "idle",
                    ngettext("Found %d scanner.", "Found %d scanners.", len(devices))
                    % len(devices),
                )
            else:
                self._form.show_devices(names, 0)
                self._form.set_device_tooltip("")
                self._form.set_device_subtitle(
                    err
                    or (
                        _("Scanner disappeared — in standby or disconnected?")
                        if outcome.vanished
                        else _("No scanners found — connect one and press Refresh.")
                    )
                )
                self._set_result_bar("idle", _("No scanners found."))
                if err:
                    self._append_log(f"[gui] {err}")
            if outcome.vanished:
                self._append_log(
                    "[gui] the selected scanner disappeared (standby or disconnected?)"
                )
        # The predicate is re-evaluated only now: a scan needs an actual
        # selected device outside a running search.
        self._update_scan_enabled()

    def _selected_device(self) -> str | None:
        """Return the SANE id of the selected device, or ``None``."""
        index = self._form.device_index()
        if 0 <= index < len(self._devices):
            return self._devices[index].get("device")
        return None

    def _on_device_selected(self) -> None:
        """A device was picked: expose its id and probe its capabilities."""
        self._form.set_device_tooltip(self._selected_device() or "")
        self._deviceflow.device_changed()

    # -------------------------------------------- capability negotiation

    def _on_source_changed(self, manual: bool) -> None:
        """The source choice changed: refine mode-dependent options.

        Whether the change is manual (a preference) or programmatic (a
        reconciliation select) is widget-callback context only the form
        has; the controller and the GTK-free flow own everything else.
        """
        self._deviceflow.source_changed(manual)

    def _render_capability_update(self, update: CapabilityUpdate) -> None:
        """Apply one flow outcome to the widgets and the log.

        The controller already started whatever probe worker the update
        requested; only rendering is left here.
        """
        if update.log_probe_failure:
            self._append_log(
                "[gui] capability probe failed; leaving all options selectable"
            )
        if update.source_blocked is not None:
            self._form.set_source_availability(update.source_blocked)
        if update.adopted_sole_source is not None:
            self._append_log(
                f"[gui] '{update.adopted_sole_source}' is the only source "
                "this scanner offers; selected it"
            )
        if update.select_source is not None:
            self._form.select_source(update.select_source)
        if update.mode_blocked is not None:
            self._form.set_mode_availability(update.mode_blocked)
        if update.refresh:
            self._form.refresh_document_hints()
            self._update_selection_block()

    def _on_choice_blocked(self, value: str, reason: str) -> None:
        """A visible-but-unavailable choice was clicked: explain, keep state."""
        self._set_result_bar("idle", _("Not available on this scanner: %s") % reason)

    def _scan_allowed(self) -> bool:
        """The one Start predicate.

        A scan needs an idle runner, a driveable CLI, an available saved
        selection and an actually selected device outside a running
        search; anything else launches work that can only fail. Advisory
        probes stay out of the predicate on purpose: Start cancels and
        joins them itself, so they never gate the button. Every trigger
        (the primary click, a menu override, a hardware button, an
        insert-to-scan edge) must consult this predicate, never widget
        sensitivity.
        """
        return (
            self._runner is None
            and not self._deviceflow.cli_blocked
            and self._selection_block_reason is None
            and not self._deviceflow.searching
            and self._selected_device() is not None
        )

    def _update_scan_enabled(self) -> None:
        """Mirror the Start predicate onto the primary action.

        The idle sensor poller follows the same predicate, but through
        the controller's own lifecycle transitions rather than from
        here: every one of its outcomes ends in a schedule attempt.
        """
        self._form.set_scan_enabled(self._scan_allowed())

    # ------------------------------------------------ idle sensor polling

    def _sensor_prefs(self) -> tuple[str, bool]:
        """The persisted trigger preferences: (button mapping, insert)."""
        return (
            hardware_button_value(self._settings),
            bool(self._settings.get("insert_to_scan")),
        )

    # ------------------------------------------------- filename preview

    def _preview_alive(self) -> bool:
        """Whether a preview may still be started or rendered."""
        return not (self._released or self._closing)

    def _preview_inputs(self) -> PreviewInputs:
        """The form values one look at the output folder is about."""
        return PreviewInputs(
            folder=Path(self._form.folder()).expanduser(),
            template=self._form.preview_template(),
            device=self._selected_device(),
        )

    def _watch_view_state(self) -> None:
        """Connect the view-state signals this runtime can deliver.

        Coming back to the window is a chance for the folder to have
        changed while nothing was watching it; going away is a reason to
        stop watching. Named handlers, because the direction decides.
        Suspension notification needs GTK 4.12: older runtimes never
        report it and keep the visibility and focus signals alone, so a
        press latched across an unreported suspension stays a documented
        gap there.
        """
        self.connect("notify::visible", self._on_visible_changed)
        self.connect("notify::is-active", self._on_active_changed)
        if hasattr(self, "is_suspended"):
            self.connect("notify::suspended", self._on_suspended_changed)

    def _on_visible_changed(self, *_args: object) -> None:
        """Watch the folder while visible, and only while visible."""
        if self.get_visible():
            self._preview.request()
        else:
            self._preview.suspend()
        self._deviceflow.view_state_changed()

    def _on_suspended_changed(self, *_args: object) -> None:
        """Compositor suspension changed: one view-state transition.

        A button latched while a still-visible window was suspended must
        resume as baseline state, never as a trigger; the controller
        re-baselines when the window becomes watchable again.
        """
        self._deviceflow.view_state_changed()

    def _on_active_changed(self, *_args: object) -> None:
        """Refresh when the window regains focus, never when it loses it.

        Losing focus is not being hidden, so the monitor stays: it is
        still the right window's folder, and dropping a working monitor
        for a visible window would only cost the next refresh.
        """
        if self.is_active():
            self._preview.request()

    def _window_suspended(self) -> bool:
        """Whether the window is currently hidden from the user.

        ``is_suspended`` needs GTK 4.12; older runtimes simply never
        report suspension.
        """
        return bool(self.is_suspended()) if hasattr(self, "is_suspended") else False

    def _sensor_trigger_allowed(self) -> bool:
        """Whether a sensor edge may start a scan right now.

        The Start predicate plus real visibility and suspension: a poll
        skips its tick while the window is hidden or suspended, so a
        read that was already in flight when it went away must not start
        a scan either. The edge is consumed rather than remembered,
        which is also what keeps a latch from firing once the window
        comes back.
        """
        return (
            self._scan_allowed()
            and bool(self.get_visible())
            and not self._window_suspended()
        )

    def _on_sensor_trigger(self, trigger: SensorTrigger) -> None:
        """One consumed sensor edge: resolve the flow, re-check, launch.

        ``same`` resolves against the form's current sheet flow at this
        moment, and the authoritative Start predicate decides again; a
        trigger it declines was already consumed and is never queued.
        """
        if self._released or not self._sensor_trigger_allowed():
            return
        flow: SheetFlow = {
            "same": self._form.sheet_flow_value(),
            "single": "single",
            "collect": "collect",
        }[trigger.mapping]
        self._append_log(f"[gui] {trigger.reason}: starting a scan")
        self._on_scan_clicked(flow)

    def _update_selection_block(self) -> None:
        """Disable Start while the active saved choice is unavailable.

        The selection is never changed silently; the user must pick another
        value themselves.
        """
        reason = self._form.selection_blocked_reason()
        self._selection_block_reason = reason
        self._update_scan_enabled()
        if reason is not None:
            self._set_result_bar(
                "idle", _("Selected option not available: %s") % reason
            )

    # ------------------------------------------------- live consequences

    def _effective_resolution(self, dpi: int) -> int | None:
        """The dpi the device would actually scan at, when it differs.

        A prepared assessment from the advisory capability snapshot, so
        the form renders the hint without importing engine internals.
        """
        assessment = assess_resolution(self._deviceflow.last_caps, dpi)
        return (
            int(assessment.effective)
            if assessment.support is Support.DEGRADED
            else None
        )

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
        store_settings(CONFIG_FILE, self._settings)

    def _on_settings_action(self, *_args: object) -> None:
        """Open the settings dialog, with the form's advanced groups."""
        dialog = build_settings_dialog(
            current_scheme=str(self._settings.get("color_scheme") or ""),
            current_ui_language=str(self._settings.get("ui_language") or ""),
            desktop_installed=desktop_entry_path().is_file(),
            on_scheme_selected=self._on_scheme_selected,
            on_ui_language_selected=lambda value: self._store_pref(
                "ui_language", value
            ),
            restart_pending=lambda: (
                str(self._settings.get("ui_language") or "")
                != self._startup_ui_language
            ),
            on_restart=self._on_restart_clicked,
            on_reset=self._on_reset_clicked,
            on_install_desktop=install_desktop_entry,
            on_remove_desktop=remove_desktop_entry,
            borrowed_groups=(
                self._form.settings_scan_group,
                self._form.settings_processing_group,
                self._form.settings_behaviour_group,
            ),
        )
        self._settings_dialog = dialog
        dialog.connect("closed", lambda *_a: setattr(self, "_settings_dialog", None))
        dialog.present(self)

    def _on_scheme_selected(self, value: str) -> None:
        """Apply and persist a color-scheme choice from the dialog."""
        self._apply_color_scheme(value)
        self._store_pref("color_scheme", value)

    def _on_hardware_button_selected(self, value: str) -> None:
        """Persist the button mapping and re-evaluate the idle poller."""
        self._store_pref("hardware_button", value)
        self._deviceflow.preferences_changed()

    def _on_insert_to_scan_toggled(self, value: bool) -> None:
        """Persist insert-to-scan and re-evaluate the idle poller."""
        self._settings["insert_to_scan"] = value
        store_settings(CONFIG_FILE, self._settings)
        self._deviceflow.preferences_changed()

    def _on_restart_clicked(self, *_args: object) -> None:
        """Ask to close, and re-execute afterwards (see ``main``)."""
        # Not on the application yet: closing may be inhibited by the
        # discard prompt and then declined, and a restart intent that
        # outlived its refused close would fire on the next ordinary quit.
        self._restart_pending = True
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
        stored = reset_settings(CONFIG_FILE)
        self._apply_saved_settings()
        # The defaults may name choices the connected scanner blocks (the
        # duplex default on a front-only feeder): negotiate again, exactly
        # like at startup, so the sole-source adoption can move the
        # selection off a blocked default. Cached snapshots make this
        # instant; without a device it is a no-op.
        self._deviceflow.settings_reset()
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
        if stored:
            self._set_result_bar("idle", _("Settings reset to defaults."))
            return
        # The window is reset either way, but the file is not: saying
        # "done" here would send the user off believing a corrupt or
        # unwanted config is gone when it comes back at the next launch.
        self._append_log(f"[gui] could not rewrite {CONFIG_FILE}")
        self._set_result_bar(
            "error",
            _("Settings reset here, but %s could not be rewritten.")
            % abbreviate_home(str(CONFIG_FILE)),
        )

    def _on_about_clicked(self, *_args: object) -> None:
        """Show a flat, single-page About dialog (no nested subpages)."""
        build_about_dialog(
            cli_version=self._deviceflow.cli_version,
            logo_file=LOGO_FILE,
            project_url=PROJECT_URL,
        ).present(self)

    # ----------------------------------------------------------- scanning

    def _on_scan_clicked(self, flow: SheetFlow = "stack") -> None:
        """Validate the output folder and launch the scan subprocess.

        ``flow`` is the sheet flow of this one run: the primary click
        passes the form's persisted choice, a menu override its one-shot
        value. Neither touches the form state.
        """
        if self._runner is not None:
            return
        folder = Path(self._form.folder()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            return
        # The scan takeover: acquisition probes the device authoritatively
        # at scan start; a still-running advisory probe would race it on
        # the same scanner (DEVICE_BUSY on some backends). The controller
        # stops the idle sensor poller first (a press latched during the
        # run must resume as baseline state, never as a trigger), then
        # cancels the advisory children and joins their workers boundedly
        # before the runner launches, clears the search latch and resets
        # the cancelled capability state. A worker thread that outlives
        # the bound is harmless: any child it still spawns carries a stale
        # generation and is killed on adoption.
        if not self._deviceflow.pause_for_scan():
            self._append_log("[gui] advisory worker still busy; child stopped")
        self._save_settings()

        persisted_flow = self._form.sheet_flow_value()
        if flow != persisted_flow:
            # A one-shot override (menu action or a mapped hardware
            # button): say so at the moment a user might wonder whether
            # they just changed a setting.
            self._append_log(
                f"[gui] one-time sheet flow for this run: {flow} "
                f"(the saved setting stays {persisted_flow})"
            )
        request = self._form.scan_request(
            self._selected_device(), folder, sheet_flow=flow
        )
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
            if not folder.is_dir():
                # The folder was created moments ago and is gone again, so
                # the child had no working directory. Pointing at the CLI
                # installation here sends the user after the wrong thing.
                self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            else:
                self._alert(
                    _("Could Not Start scanmole"),
                    f"{exc}\n\n" + _("Install the scanmole CLI somewhere in PATH."),
                )
            # The takeover tore down device coordination for a scan that
            # never started; resume it now rather than waiting for the
            # quiet presence poll, whose unchanged result would skip the
            # negotiation for good. The resume negotiates first, so
            # sensor polling cannot rearm from the retained caps, whose
            # source settings the reset made underivable (a poll would
            # read the backend's default source).
            self._deviceflow.resume_after_scan()
            self._update_scan_enabled()
            return
        self._runner = runner
        self._set_result_bar("running", _("Starting scanmole\u2026"))
        self._form.set_running(True)

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
        render_session_update(
            self._session,
            update,
            lambda title: self._set_result_bar("running", title),
            self._append_log,
            set_waiting_bar=self._set_waiting_bar,
        )

    def _set_waiting_bar(self, title: str, manual: bool) -> None:
        """Show the collect wait: spinner text plus Finish (and Next Sheet)."""
        self._set_result_bar("running", title)
        self._status.show_wait_actions(next_sheet=manual)

    def _on_next_sheet(self) -> None:
        """The Next Sheet action: one control line to the collect run."""
        runner = self._runner
        if runner is None or not runner.next_sheet():
            self._append_log("[gui] next-sheet request had no waiting scan")

    def _on_finish_collect(self) -> None:
        """The Finish action: finalize the collect run after this segment."""
        runner = self._runner
        if runner is not None and runner.finish():
            self._set_result_bar("running", _("Finishing…"))
            self._append_log("[gui] finish requested")

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
        self._form.set_running(False)
        self._update_scan_enabled()
        # The run either produced the previewed file or freed nothing;
        # either way the next name is now a different question.
        self._preview.request()
        self._append_log(f"[gui] scanmole exited with code {exit_code}")
        # The scan takeover reset the capability flow; renegotiate the
        # selected device's availability now that it is free again.
        self._deviceflow.resume_after_scan()

        outcome = complete(self._session, exit_code, self._run_folder)
        if outcome.kind == "cancelled":
            self._set_result_bar("idle", _("Scan cancelled."))
            return
        if outcome.kind == "success":
            if outcome.output is not None:
                self._last_output = outcome.output
                # The advice lands where the user is looking at what it
                # would have shrunk, so it is worth repeating here even
                # though the form already carries it.
                self._set_result_bar(
                    "success",
                    success_summary(outcome.pages, outcome.blanks),
                    outcome.output.name,
                    JBIG2_HINT if self._form.jbig2_hint_applies() else "",
                )
            else:
                self._set_result_bar("idle", _("Finished."))
            return
        heading, body = exit_failure_texts(outcome.exit_code, outcome.error_message)
        self._set_result_bar("error", heading)
        self._alert(heading, body)

    # ------------------------------------------------------------- cancel

    def _on_cancel_clicked(self, *_args: object) -> None:
        """Terminate the scan's process group, escalating to SIGKILL."""
        runner = self._runner
        if runner is None or not runner.cancel():
            return  # no run, already finished, or already cancelling
        self._session = mark_cancelled(self._session)
        self._form.set_cancel_enabled(False)
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
        when no scan ran in between. A window reporting a nonpositive
        size is destroyed or unrealized; nothing it reports is real
        state, so its geometry is not stored.
        """
        if self.get_width() > 0 and self.get_height() > 0:
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
        scan's process group on this thread instead. After a normal close
        the window is already released and destroyed: the close request
        persisted against the live widgets, and reading the dead window
        here would overwrite that snapshot with zeros and defaults.
        """
        self._preview.stop()
        if not self._released:
            self._persist_ui_state()
        self._released = True
        self._deviceflow.stop()
        runner = self._runner
        if runner is not None:
            runner.shutdown()

    def _close_discards_pages(self) -> bool:
        """Whether closing now would throw away captured pages.

        Only a live run that already produced something qualifies. A scan
        that has not delivered a page yet loses nothing worth a prompt,
        and neither does an idle window, so the question is never asked
        for the sake of asking.
        """
        runner = self._runner
        return (
            runner is not None
            and runner.is_running()
            and self._session.pages > 0
            and not self._closing
            and not self._close_confirmed
        )

    def _confirm_close(self) -> None:
        """Ask before discarding a run that already captured pages."""
        heading, body = close_confirmation_text(self._session)
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("keep", _("Keep Scanning"))
        dialog.add_response("close", _("Close Anyway"))
        dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("keep")
        dialog.set_close_response("keep")
        dialog.connect("response", self._on_close_confirm_response)
        dialog.present(self)

    def _on_close_confirm_response(self, _dialog: object, response: str) -> None:
        """Close for real when the discard was confirmed."""
        if response != "close":
            # "Keep Scanning" and a dismissed dialog (its close response)
            # both land here: the window stays, so any restart it was
            # asking for is off.
            self._restart_pending = False
            return
        self._close_confirmed = True
        # The engine preserves the pages and logs the recovery command,
        # but the log pane dies with this window: echo what still arrives
        # to the terminal, or the only path to those pages is lost.
        self._echo_log = True
        self.close()

    def _on_close_request(self, *_args: object) -> bool:
        """Persist the form and window geometry, stop any running scan."""
        if self._close_discards_pages():
            # Nothing is torn down yet: the answer may well be "keep
            # scanning", and a half-released window cannot resume.
            self._confirm_close()
            return True  # inhibit until the user decides
        if self._restart_pending:
            # The close is going through, so the intent may leave the
            # window; ``main`` re-executes once the loop ends.
            application = self.get_application()
            if application is not None:
                application.restart_requested = True
        self._persist_ui_state()
        # No advisory child may outlive the window, and no late advisory
        # result may touch it while it is closing.
        self._released = True
        self._deviceflow.stop()
        self._preview.stop()
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

    def _alert(self, heading: str, body: str) -> None:
        """Present a simple modal alert dialog."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def _on_pick_folder(self) -> None:
        """Open a folder chooser for the output directory."""
        dialog = Gtk.FileDialog(title=_("Choose Output Folder"), modal=True)
        folder = self._form.folder()
        if folder and Path(folder).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(folder))
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
            self._form.set_folder(gfile.get_path())

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
        # Own reference: props.active_window stays None until the window
        # received focus, which an early Ctrl+C beats. The shutdown paths
        # must find the window regardless.
        self.window: MainWindow | None = None
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
        window = self.props.active_window or self.window
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
        self.window = win
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
        # conventional exit code instead of a traceback. An interrupt that
        # lands while the main thread runs Python code (e.g. during
        # startup) aborts app.run() without the shutdown signal, so the
        # synchronous barrier must run here too: without it a wedged
        # advisory probe child would outlive the GUI holding the scanner.
        window = app.props.active_window or app.window
        shutdown_now = getattr(window, "_shutdown_now", None)
        if shutdown_now is not None:
            shutdown_now()
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
