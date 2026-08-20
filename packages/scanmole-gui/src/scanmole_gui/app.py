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
import threading
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
from scanmole.external import run_command  # noqa: E402  # supervised capture
from scanmole.negotiation import (  # noqa: E402
    ADVISORY_PROBE_TIMEOUT_SECONDS,
    Support,
    assess_resolution,
    probe_snapshot,
)
from scanmole_gui import __version__, desktop  # noqa: E402
from scanmole_gui.discovery import (  # noqa: E402
    display_name,
    evaluate_listing,
    parse_version,
)
from scanmole_gui.form import ScanForm, default_folder  # noqa: E402
from scanmole_gui.i18n import _, ngettext  # noqa: E402  # after gi setup
from scanmole_gui.probing import (  # noqa: E402
    CapabilityFlow,
    CapabilityUpdate,
    ProbeRequest,
)
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
from scanmole_gui.settings import load_settings, store_settings  # noqa: E402
from scanmole_gui.widgets import combo_select, combo_value  # noqa: E402

LOGGER = logging.getLogger(__name__)

APP_ID = "com.foundata.ScanMole"
PROJECT_URL = "https://foundata.com/en/projects/scanmole/"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "scanmole" / "gui.json"
ICON_DIR = Path(__file__).resolve().parent / "icons"
LOGO_FILE = ICON_DIR / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"

# Endonyms on purpose: a language name is most recognizable in itself.
UI_LANGUAGES = ((_("System default"), ""), ("English", "en"), ("Deutsch", "de"))
COLOR_SCHEMES = ((_("System default"), ""), (_("Light"), "light"), (_("Dark"), "dark"))

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
        self._flow = CapabilityFlow()
        self._selection_block_reason: str | None = None
        self._devices: list[dict[str, str]] = []
        self._run_folder = Path(default_folder())
        self._last_output: Path | None = None
        self._cli_version: str | None = None
        # Set from the hello handshake: a truthy value blocks scanning because
        # the CLI's major version does not match this GUI (see ARCHITECTURE.md).
        self._cli_blocked = False
        self._version_alert_shown = False
        self._settings_dialog: Adw.PreferencesDialog | None = None
        # The language this process actually runs with; a differing persisted
        # value means a restart is pending.
        self._startup_ui_language = str(self._settings.get("ui_language") or "")

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

        self._form = ScanForm(
            on_device_selected=self._on_device_selected,
            on_source_changed=self._on_source_changed,
            on_refresh=self._refresh_devices,
            on_scan=self._on_scan_clicked,
            on_cancel=self._on_cancel_clicked,
            on_pick_folder=self._on_pick_folder,
            on_more_languages=self._on_more_languages,
            on_choice_blocked=self._on_choice_blocked,
            device_for_preview=self._selected_device,
            effective_resolution=self._effective_resolution,
        )
        self._build_log_area()

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

        self._form.refresh_document_hints()

    def _apply_layout(self, *, wide: bool) -> None:
        """Arrange the form sections in one column or a two-column grid."""
        grid_cells = (
            (self._form.scanner_group, 0, 0, 1),
            (self._form.output_group, 1, 0, 1),
            (self._form.document_group, 0, 1, 1),
            (self._form.processing_group, 1, 1, 1),
            (self._log_area, 0, 2, 2),  # spans both columns
        )
        sections_narrow = (
            self._form.scanner_group,
            self._form.output_group,
            self._form.document_group,
            self._form.processing_group,
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
        """Restore the form and application look from the persisted settings."""
        self._flow.preferred_source = str(self._settings.get("source", "adf-duplex"))
        self._form.apply_settings(self._settings)
        self._apply_color_scheme(str(self._settings.get("color_scheme") or ""))

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
                self._form.select_language(code)

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
            "source": self._flow.preferred_source,
            **self._form.persisted_values(),
        }
        store_settings(CONFIG_FILE, self._settings)

    # ----------------------------------------------------------- devices

    def _refresh_devices(self, *_args: object) -> None:
        """Start an asynchronous ``scanmole --list-devices`` in a worker thread."""
        if self._runner is not None or self._searching:
            return
        self._searching = True
        self._form.set_refresh_enabled(False)
        self._form.set_device_subtitle(_("Searching for scanners…"))
        prefer = self._selected_device() or str(self._settings.get("device") or "")
        threading.Thread(
            target=self._devices_worker, args=(prefer,), daemon=True
        ).start()

    def _devices_worker(self, prefer: str) -> None:
        """Worker thread: query devices and hand results back to the main loop.

        The parsing and the compatibility decision live in the GTK-free
        :mod:`scanmole_gui.discovery`; this thread only runs the command
        (through the supervised engine helper, so a wedged backend probe
        cannot leave descendants behind on timeout) and translates the
        typed outcome for the user.
        """
        if self._cli_version is None:
            self._cli_version = self._probe_cli_version()
        devices: list[dict[str, str]] = []
        err = ""
        try:
            result = run_command(
                [self._scanmole, "--list-devices", "--json"],
                timeout_seconds=120,
            )
            if result.stderr.strip():
                GLib.idle_add(self._append_log, result.stderr.strip())
            listing = evaluate_listing(result.stdout, result.returncode)
            if listing.cli_version is not None:
                self._cli_version = listing.cli_version
            self._cli_blocked = listing.needed is not None
            devices = listing.devices
            if listing.needed is not None:
                err = _(
                    "Incompatible scanmole CLI: found version %(found)s, "
                    "but this GUI needs %(needed)s."
                ) % {
                    "found": listing.cli_version or _("unknown"),
                    "needed": listing.needed,
                }
            elif listing.failed_exit is not None:
                err = _("Device search failed (exit %(code)d).") % {
                    "code": listing.failed_exit
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
        return parse_version(result.stdout)

    def _apply_devices(
        self, devices: list[dict[str, str]], err: str, prefer: str
    ) -> None:
        """Populate the device dropdown on the main loop."""
        self._searching = False
        self._form.set_refresh_enabled(self._runner is None)
        self._form.set_scan_enabled(
            self._runner is None
            and not self._cli_blocked
            and self._selection_block_reason is None
        )
        if self._cli_blocked and err and not self._version_alert_shown:
            self._version_alert_shown = True
            self._alert(_("Incompatible scanmole CLI"), err)
        self._devices = devices
        names = [display_name(device, _("Unknown device")) for device in devices]
        # The row itself must stay sensitive either way: disabling it would
        # also disable its refresh-button suffix, leaving no way to rescan.
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
            self._start_negotiation()
        else:
            self._form.show_devices(names, 0)
            self._form.set_device_tooltip("")
            self._form.set_device_subtitle(
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
        index = self._form.device_index()
        if 0 <= index < len(self._devices):
            return self._devices[index].get("device")
        return None

    def _on_device_selected(self) -> None:
        """A device was picked: expose its id and probe its capabilities."""
        self._form.set_device_tooltip(self._selected_device() or "")
        self._start_negotiation()

    # -------------------------------------------- capability negotiation

    def _on_source_changed(self, manual: bool) -> None:
        """The source choice changed: refine mode-dependent options.

        Whether the change is manual (a preference) or programmatic (a
        reconciliation select) is widget-callback context only the form
        has; the GTK-free flow owns everything else.
        """
        update = self._flow.change_source(
            self._selected_device(),
            self._runner is not None,
            self._form.source_value(),
            manual=manual,
        )
        self._render_capability_update(update)

    def _start_negotiation(self) -> None:
        """Kick off an advisory capability probe for the selected device.

        Two stages: a bare probe derives source availability, a follow-up
        with the negotiated source applied refines the mode-dependent
        options. The GTK-free flow serializes probes, drops stale results
        and never probes while a scan owns the device. Advisory only: the
        engine re-negotiates before every scan.
        """
        update = self._flow.select_device(
            self._selected_device(),
            self._runner is not None,
            self._form.source_value(),
        )
        self._render_capability_update(update)

    def _probe_worker(self, token: int, request: ProbeRequest) -> None:
        snapshot = probe_snapshot(
            request.device, request.settings, ADVISORY_PROBE_TIMEOUT_SECONDS
        )
        GLib.idle_add(self._on_probe_done, token, request, snapshot)

    def _on_probe_done(
        self, token: int, request: ProbeRequest, snapshot: object
    ) -> None:
        update = self._flow.probe_completed(
            token, request, snapshot, self._selected_device(), self._form.source_value()
        )
        self._render_capability_update(update)

    def _render_capability_update(self, update: CapabilityUpdate) -> None:
        """Apply one flow outcome to the widgets, log and workers."""
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
        if update.start_probe is not None:
            token, request = update.start_probe
            threading.Thread(
                target=self._probe_worker, args=(token, request), daemon=True
            ).start()
        if update.refresh:
            self._form.refresh_document_hints()
            self._update_selection_block()

    def _on_choice_blocked(self, value: str, reason: str) -> None:
        """A visible-but-unavailable choice was clicked: explain, keep state."""
        self._set_result_bar("idle", _("Not available on this scanner: %s") % reason)

    def _update_selection_block(self) -> None:
        """Disable Start while the active saved choice is unavailable.

        The selection is never changed silently; the user must pick another
        value themselves.
        """
        reason = self._form.selection_blocked_reason()
        self._selection_block_reason = reason
        self._form.set_scan_enabled(
            self._runner is None and not self._cli_blocked and reason is None
        )
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
        assessment = assess_resolution(self._flow.last_caps, dpi)
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
        store_settings(CONFIG_FILE, self._settings)
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

    def _on_scan_clicked(self, *_args: object) -> None:
        """Validate the output folder and launch the scan subprocess."""
        if self._runner is not None:
            return
        folder = Path(self._form.folder()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            return
        self._save_settings()

        request = self._form.scan_request(self._selected_device(), folder)
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
        self._form.set_running(False)
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
