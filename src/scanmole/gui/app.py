"""ScanMole: GTK4/libadwaita frontend for the ``scanmole`` CLI.

A deliberately thin GUI: it builds a ``scanmole --json`` command line from the
form, streams the CLI's JSON-lines events into a progress area and offers the
finished PDF. All scanning and OCR work happens in the ``scanmole`` executable
(resolved from ``PATH``).

Widget labels, status texts and dialogs are translatable via gettext (see
:mod:`scanmole.gui.i18n`); the log pane stays English on purpose, because it
mixes in output of the English-only CLI.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402  # after require_version

from scanmole.gui.i18n import _, ngettext  # noqa: E402  # after gi setup

APP_ID = "com.foundata.ScanMole"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "scanmole" / "gui.json"

SOURCES = (
    (_("ADF Duplex"), "adf-duplex"),
    (_("ADF Front"), "adf"),
    (_("ADF Back"), "adf-back"),
    (_("Flatbed"), "flatbed"),
)
MODES = ((_("Lineart"), "lineart"), (_("Gray"), "gray"), (_("Color"), "color"))
RESOLUTIONS = tuple((f"{dpi} dpi", str(dpi)) for dpi in (150, 200, 300, 400, 600))
PAGE_SIZES = (
    ("A4", "a4"),
    ("A5", "a5"),
    ("A6", "a6"),
    (_("Letter"), "letter"),
    (_("Legal"), "legal"),
)

# Friendly texts for the CLI's documented exit codes.
EXIT_HINTS: dict[int, tuple[str, str]] = {
    2: (
        _("No Pages Scanned"),
        _(
            "No pages were scanned — is the ADF loaded?\n"
            "(All pages may also have been detected as blank.)"
        ),
    ),
    3: (
        _("Scanner Error"),
        _(
            "The scanner reported an error. Make sure it is connected, powered "
            "on and not in use by another application, then try again."
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
            "PDF assembly or OCR failed after scanning. "
            "See the log for details."
        ),
    ),
}

SIGKILL_GRACE_SECONDS = 3  # between SIGTERM and SIGKILL on cancel


def find_scanmole() -> str:
    """Return the ``scanmole`` executable on ``PATH``, or the bare name."""
    return shutil.which("scanmole") or "scanmole"


def default_folder() -> str:
    """Return the XDG Documents folder, falling back to the home directory."""
    docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    return docs or str(Path.home())


def load_settings() -> dict[str, object]:
    """Load persisted GUI settings, returning an empty dict on any error."""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def store_settings(data: dict[str, object]) -> None:
    """Persist GUI settings; failures are ignored as a convenience feature."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def combo_value(row: Adw.ComboRow, items: tuple[tuple[str, str], ...]) -> str:
    """Return the CLI value for a combo row's selected ``(label, value)`` item."""
    index = row.get_selected()
    return items[index][1] if 0 <= index < len(items) else items[0][1]


def combo_select(
    row: Adw.ComboRow, items: tuple[tuple[str, str], ...], value: str
) -> None:
    """Select the combo row item whose CLI value equals ``value``."""
    for index, (_label, item_value) in enumerate(items):
        if item_value == value:
            row.set_selected(index)
            return


def as_int(value: object, fallback: int) -> int:
    """Return ``value`` as an int when it is one, else ``fallback``.

    JSON event fields are untrusted input; plural forms need real ints.
    """
    return value if isinstance(value, int) else fallback


class MainWindow(Adw.ApplicationWindow):
    """The single application window: scan form, progress area and results."""

    def __init__(self, **kwargs: object) -> None:
        """Build the UI, restore settings and start device discovery."""
        super().__init__(**kwargs)
        self.set_title("ScanMole")
        self.set_default_size(620, 840)

        self._scanmole = find_scanmole()
        self._settings = load_settings()

        # Runtime state
        self._proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._devices: list[dict[str, str]] = []
        self._pages = 0
        self._blanks = 0
        self._result: dict[str, object] = {}
        self._error_message: str | None = None
        self._run_folder = Path(default_folder())
        self._last_output: Path | None = None
        self._folder = str(self._settings.get("folder") or default_folder())

        self._build_ui()
        self._apply_saved_settings()
        self.connect("close-request", self._on_close_request)

        self._refresh_devices()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        """Assemble the header bar, form groups and progress area."""
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="ScanMole"))
        toolbar.add_top_bar(header)

        self._refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh devices")
        )
        self._refresh_btn.connect("clicked", self._refresh_devices)
        header.pack_start(self._refresh_btn)

        self._scan_btn = Gtk.Button(label=_("Scan"))
        self._scan_btn.add_css_class("suggested-action")
        self._scan_btn.connect("clicked", self._on_scan_clicked)
        header.pack_end(self._scan_btn)

        self._cancel_btn = Gtk.Button(label=_("Cancel"), visible=False)
        self._cancel_btn.add_css_class("destructive-action")
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_end(self._cancel_btn)

        self._toasts = Adw.ToastOverlay()
        toolbar.set_content(self._toasts)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        self._toasts.set_child(scroller)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480)
        scroller.set_child(clamp)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=18,
            margin_bottom=18,
            margin_start=16,
            margin_end=16,
        )
        clamp.set_child(box)

        box.append(self._build_result_group())

        scanner_grp = Adw.PreferencesGroup(title=_("Scanner"))
        self._device_row = Adw.ComboRow(
            title=_("Device"),
            sensitive=False,
            subtitle=_("Searching for scanners…"),
        )
        self._device_row.connect("notify::selected", self._on_device_selected)
        scanner_grp.add(self._device_row)
        self._source_row = self._make_combo(scanner_grp, _("Source"), SOURCES)
        box.append(scanner_grp)

        doc_grp = Adw.PreferencesGroup(title=_("Document"))
        self._mode_row = self._make_combo(doc_grp, _("Color Mode"), MODES)
        self._res_row = self._make_combo(doc_grp, _("Resolution"), RESOLUTIONS)
        self._size_row = self._make_combo(doc_grp, _("Page Size"), PAGE_SIZES)
        box.append(doc_grp)

        proc_grp = Adw.PreferencesGroup(title=_("Processing"))
        self._ocr_row = Adw.SwitchRow(
            title=_("OCR"), subtitle=_("Make the PDF text-searchable"), active=True
        )
        self._ocr_row.connect("notify::active", self._on_ocr_toggled)
        proc_grp.add(self._ocr_row)
        self._lang_row = Adw.EntryRow(title=_("OCR Language"))
        self._lang_row.set_text("deu")
        self._lang_row.set_tooltip_text(
            _('Tesseract language codes — combine with "+", e.g. "deu+eng"')
        )
        proc_grp.add(self._lang_row)
        self._blank_row = Adw.SwitchRow(
            title=_("Skip Blank Pages"),
            subtitle=_("Remove pages detected as empty"),
            active=True,
        )
        proc_grp.add(self._blank_row)
        box.append(proc_grp)

        out_grp = Adw.PreferencesGroup(title=_("Output"))
        self._folder_row = Adw.ActionRow(title=_("Folder"), subtitle=self._folder)
        folder_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Choose output folder"),
        )
        folder_btn.add_css_class("flat")
        folder_btn.connect("clicked", self._on_pick_folder)
        self._folder_row.add_suffix(folder_btn)
        self._folder_row.set_activatable_widget(folder_btn)
        out_grp.add(self._folder_row)
        self._name_row = Adw.EntryRow(title=_("Filename (optional)"))
        self._name_row.set_tooltip_text(
            _('Leave empty for an automatic name; ".pdf" is appended if missing')
        )
        out_grp.add(self._name_row)
        box.append(out_grp)

        self._form_groups = (scanner_grp, doc_grp, proc_grp, out_grp)

        box.append(self._build_progress_area())

    def _build_result_group(self) -> Adw.PreferencesGroup:
        """Build the (initially hidden) saved-file result group."""
        self._result_group = Adw.PreferencesGroup(visible=False)
        self._result_row = Adw.ActionRow(title=_("Saved"))
        self._result_row.set_subtitle_selectable(True)
        show_btn = Gtk.Button(label=_("Show in Folder"), valign=Gtk.Align.CENTER)
        show_btn.connect("clicked", lambda *_args: self._show_in_folder())
        self._result_row.add_suffix(show_btn)
        open_btn = Gtk.Button(label=_("Open"), valign=Gtk.Align.CENTER)
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", lambda *_args: self._open_output())
        self._result_row.add_suffix(open_btn)
        self._result_row.set_activatable_widget(open_btn)
        self._result_group.add(self._result_row)
        return self._result_group

    def _build_progress_area(self) -> Gtk.Box:
        """Build the status label, pulsing progress bar and log expander."""
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._status_label = Gtk.Label(label=_("Ready."), xalign=0.0, wrap=True)
        area.append(self._status_label)
        self._bar = Gtk.ProgressBar(visible=False)
        area.append(self._bar)

        expander = Gtk.Expander(label=_("Log"))
        log_scroller = Gtk.ScrolledWindow(min_content_height=170, has_frame=True)
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
        expander.set_child(log_scroller)
        area.append(expander)
        return area

    def _make_combo(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        items: tuple[tuple[str, str], ...],
    ) -> Adw.ComboRow:
        """Add a combo row listing the labels of ``items`` to ``group``."""
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new([label for label, _value in items]))
        group.add(row)
        return row

    # ------------------------------------------------------- settings I/O

    def _apply_saved_settings(self) -> None:
        """Restore form widgets from the persisted settings."""
        settings = self._settings
        combo_select(
            self._source_row, SOURCES, str(settings.get("source", "adf-duplex"))
        )
        combo_select(self._mode_row, MODES, str(settings.get("mode", "lineart")))
        combo_select(self._res_row, RESOLUTIONS, str(settings.get("resolution", "300")))
        combo_select(self._size_row, PAGE_SIZES, str(settings.get("page_size", "a4")))
        self._ocr_row.set_active(bool(settings.get("ocr", True)))
        self._lang_row.set_text(str(settings.get("lang", "deu")))
        self._lang_row.set_sensitive(self._ocr_row.get_active())
        self._blank_row.set_active(bool(settings.get("skip_blanks", True)))

    def _save_settings(self) -> None:
        """Snapshot the current form into the settings file."""
        self._settings = {
            "device": self._selected_device() or "",
            "source": combo_value(self._source_row, SOURCES),
            "mode": combo_value(self._mode_row, MODES),
            "resolution": combo_value(self._res_row, RESOLUTIONS),
            "page_size": combo_value(self._size_row, PAGE_SIZES),
            "ocr": self._ocr_row.get_active(),
            "lang": self._lang_row.get_text().strip(),
            "skip_blanks": self._blank_row.get_active(),
            "folder": self._folder,
        }
        store_settings(self._settings)

    # ----------------------------------------------------------- devices

    def _refresh_devices(self, *_args: object) -> None:
        """Start an asynchronous ``scanmole --list-devices`` in a worker thread."""
        if self._proc is not None:
            return
        self._refresh_btn.set_sensitive(False)
        self._device_row.set_subtitle(_("Searching for scanners…"))
        prefer = self._selected_device() or str(self._settings.get("device") or "")
        threading.Thread(
            target=self._devices_worker, args=(prefer,), daemon=True
        ).start()

    def _devices_worker(self, prefer: str) -> None:
        """Worker thread: query devices and hand results back to the main loop."""
        devices: list[dict[str, str]] = []
        err = ""
        try:
            result = subprocess.run(
                [self._scanmole, "--list-devices", "--json"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            for raw in result.stdout.splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("event") == "devices":
                    # Hide virtual devices (webcams, SANE test backend).
                    devices = [
                        device
                        for device in event.get("devices") or []
                        if not device.get("device", "").startswith(("v4l:", "test:"))
                    ]
            if result.stderr.strip():
                GLib.idle_add(self._append_log, result.stderr.strip())
            if result.returncode != 0 and not devices:
                err = _("Device search failed (exit %(code)d).") % {
                    "code": result.returncode
                }
        except FileNotFoundError:
            err = _("scanmole CLI not found — install it or add it to PATH.")
        except subprocess.TimeoutExpired:
            err = _("Device search timed out.")
        except OSError as exc:
            err = _("Device search failed: %(error)s") % {"error": exc}
        GLib.idle_add(self._apply_devices, devices, err, prefer)

    def _apply_devices(
        self, devices: list[dict[str, str]], err: str, prefer: str
    ) -> None:
        """Populate the device dropdown on the main loop."""
        self._refresh_btn.set_sensitive(self._proc is None)
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
        if devices:
            self._device_row.set_sensitive(True)
            index = next(
                (i for i, d in enumerate(devices) if d.get("device") == prefer), 0
            )
            self._device_row.set_selected(index)
            self._device_row.set_subtitle(devices[index].get("device", ""))
            self._set_status(
                ngettext("Found %d scanner.", "Found %d scanners.", len(devices))
                % len(devices)
            )
        else:
            self._device_row.set_sensitive(False)
            self._device_row.set_subtitle(
                err or _("No scanners found — connect one and press Refresh.")
            )
            self._set_status(_("No scanners found."))
            if err:
                self._append_log(f"[gui] {err}")

    def _selected_device(self) -> str | None:
        """Return the SANE id of the selected device, or ``None``."""
        index = self._device_row.get_selected()
        if 0 <= index < len(self._devices):
            return self._devices[index].get("device")
        return None

    def _on_device_selected(self, *_args: object) -> None:
        """Show the selected device's SANE id as the row subtitle."""
        self._device_row.set_subtitle(self._selected_device() or "")

    # ----------------------------------------------------------- scanning

    def _build_argv(self, folder: Path) -> list[str]:
        """Build the ``scanmole --json`` command line from the current form."""
        argv = [self._scanmole, "--json"]
        device = self._selected_device()
        if device:
            argv += ["-d", device]
        argv += [
            "--source",
            combo_value(self._source_row, SOURCES),
            "--mode",
            combo_value(self._mode_row, MODES),
            "-r",
            combo_value(self._res_row, RESOLUTIONS),
            "--page-size",
            combo_value(self._size_row, PAGE_SIZES),
        ]
        if self._ocr_row.get_active():
            argv.append("--ocr")
            lang = self._lang_row.get_text().strip()
            if lang:
                argv += ["-l", lang]
        else:
            argv.append("--no-ocr")
        if not self._blank_row.get_active():
            argv.append("--keep-blanks")
        name = self._name_row.get_text().strip()
        if name:
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            argv += ["-o", str(folder / name)]
        # With no -o, scanmole picks its default name; the process runs with
        # cwd=folder so that name lands in the chosen output folder.
        return argv

    def _on_scan_clicked(self, *_args: object) -> None:
        """Validate the output folder and launch the scan subprocess."""
        if self._proc is not None:
            return
        folder = Path(self._folder).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(_("Cannot Create Output Folder"), f"{folder}\n\n{exc}")
            return
        self._save_settings()

        self._pages = self._blanks = 0
        self._result = {}
        self._error_message = None
        self._cancelled = False
        self._run_folder = folder
        self._last_output = None
        self._result_group.set_visible(False)

        argv = self._build_argv(folder)
        self._append_log("$ " + shlex.join(argv))
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(folder),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,  # own process group -> clean killpg
            )
        except OSError as exc:
            self._append_log(f"[gui] failed to start scanmole: {exc}")
            self._alert(
                _("Could Not Start scanmole"),
                f"{exc}\n\n" + _("Install the scanmole CLI somewhere in PATH."),
            )
            return
        self._proc = proc
        self._set_status(_("Starting scanmole…"))
        self._set_running(True)
        threading.Thread(target=self._supervise, args=(proc,), daemon=True).start()

    def _supervise(self, proc: subprocess.Popen[str]) -> None:
        """Worker thread: pump both pipes, wait, then report back."""
        t_out = threading.Thread(
            target=self._pump,
            args=(proc.stdout, self._on_stdout_line, proc),
            daemon=True,
        )
        t_err = threading.Thread(
            target=self._pump,
            args=(proc.stderr, self._on_stderr_line, proc),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        rc = proc.wait()
        t_out.join(timeout=5)  # pipes hit EOF at process exit
        t_err.join(timeout=5)
        GLib.idle_add(self._on_process_exit, proc, rc)

    @staticmethod
    def _pump(stream: object, handler: object, proc: object) -> None:
        """Forward each line from ``stream`` to ``handler`` on the main loop."""
        try:
            for line in stream:  # type: ignore[attr-defined]
                GLib.idle_add(handler, proc, line)
        except (OSError, ValueError):
            pass

    # -------------------------------------------------- JSON event stream

    def _on_stdout_line(self, proc: object, line: str) -> None:
        """Interpret one JSON event line from the CLI."""
        if proc is not self._proc:
            return  # stale run
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            self._append_log(line)  # non-JSON on stdout: just log it
            return
        kind = event.get("event")
        if kind == "start":
            self._set_status(_("Scanning…"))
        elif kind == "page":
            self._pages = as_int(event.get("n"), self._pages + 1)
            if event.get("blank") and self._blank_row.get_active():
                self._blanks += 1
            text = _("Page %d scanned") % self._pages
            if self._blanks:
                text += (
                    ngettext(
                        " (%d blank skipped)", " (%d blanks skipped)", self._blanks
                    )
                    % self._blanks
                )
            self._set_status(text + "…")
        elif kind == "scan_done":
            total = as_int(event.get("total"), self._pages)
            kept = as_int(event.get("kept"), max(self._pages - self._blanks, 0))
            self._set_status(
                ngettext(
                    "Scan finished — keeping %(kept)d of %(total)d page…",
                    "Scan finished — keeping %(kept)d of %(total)d pages…",
                    total,
                )
                % {"kept": kept, "total": total}
            )
        elif kind == "ocr_start":
            self._set_status(_("Running OCR…"))
        elif kind == "done":
            self._result = event
        elif kind == "error":
            self._error_message = str(event.get("message") or _("Unknown error"))
            self._append_log(f"[error] {self._error_message}")

    def _on_stderr_line(self, _proc: object, line: str) -> None:
        """Append a raw stderr line to the log view."""
        line = line.rstrip("\n")
        if line:
            self._append_log(line)

    # ------------------------------------------------------- process exit

    def _on_process_exit(self, proc: object, rc: int) -> None:
        """Finalize the UI when the scan subprocess exits."""
        if proc is not self._proc:
            return
        self._proc = None
        self._set_running(False)
        self._append_log(f"[gui] scanmole exited with code {rc}")

        if self._cancelled:
            self._set_status(_("Scan cancelled."))
            self._toasts.add_toast(Adw.Toast(title=_("Scan cancelled")))
            return
        if rc == 0:
            output = self._result.get("output")
            pages = as_int(self._result.get("pages"), self._pages)
            if output:
                path = Path(str(output))
                if not path.is_absolute():
                    path = self._run_folder / path
                self._set_status(
                    ngettext(
                        "Done — saved %(pages)d page to %(name)s.",
                        "Done — saved %(pages)d pages to %(name)s.",
                        pages,
                    )
                    % {"pages": pages, "name": path.name}
                )
                self._show_result(path)
            else:
                self._set_status(_("Finished."))
            return
        heading, body = EXIT_HINTS.get(
            rc,
            (
                _("Scan Failed"),
                _("scanmole exited with status %(code)d. See the log for details.")
                % {"code": rc},
            ),
        )
        if self._error_message:
            body = body + "\n\n" + _("Details:") + " " + self._error_message
        self._set_status(_("Scan failed."))
        self._alert(heading, body)

    def _show_result(self, path: Path) -> None:
        """Reveal the result row and show a toast for the saved PDF."""
        self._last_output = path
        self._result_row.set_title(_("Saved: %s") % path.name)
        self._result_row.set_subtitle(str(path))
        self._result_group.set_visible(True)
        toast = Adw.Toast(title=_("Saved: %s") % path.name)
        toast.set_button_label(_("Open"))
        toast.connect("button-clicked", lambda *_args: self._open_output())
        self._toasts.add_toast(toast)

    # ------------------------------------------------------------- cancel

    def _on_cancel_clicked(self, *_args: object) -> None:
        """Terminate the scan's process group, escalating to SIGKILL."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._cancelled = True
        self._cancel_btn.set_sensitive(False)
        self._set_status(_("Cancelling…"))
        self._append_log("[gui] cancelling — SIGTERM to process group")
        self._signal_group(proc, signal.SIGTERM)
        GLib.timeout_add_seconds(SIGKILL_GRACE_SECONDS, self._sigkill_if_alive, proc)

    def _sigkill_if_alive(self, proc: subprocess.Popen[str]) -> bool:
        """SIGKILL the process group if it survived the SIGTERM grace period."""
        if proc.poll() is None:
            self._append_log("[gui] still running — SIGKILL to process group")
            self._signal_group(proc, signal.SIGKILL)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _signal_group(proc: subprocess.Popen[str], sig: int) -> None:
        """Send ``sig`` to the subprocess's process group (pid == pgid)."""
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _on_close_request(self, *_args: object) -> bool:
        """Terminate any running scan before letting the window close."""
        if self._proc is not None and self._proc.poll() is None:
            self._signal_group(self._proc, signal.SIGTERM)
        return False  # allow the window to close

    # -------------------------------------------------------- UI plumbing

    def _set_running(self, running: bool) -> None:
        """Toggle the form and header buttons for a running scan."""
        self._scan_btn.set_visible(not running)
        self._cancel_btn.set_visible(running)
        self._cancel_btn.set_sensitive(True)
        self._refresh_btn.set_sensitive(not running)
        for group in self._form_groups:
            group.set_sensitive(not running)
        self._bar.set_visible(running)
        if running:
            GLib.timeout_add(120, self._pulse)

    def _pulse(self) -> bool:
        """Advance the indeterminate progress bar while a scan runs."""
        if self._proc is None:
            self._bar.set_visible(False)
            return GLib.SOURCE_REMOVE
        self._bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _set_status(self, text: str) -> None:
        """Set the status label text."""
        self._status_label.set_text(text)

    def _append_log(self, text: str) -> None:
        """Append a line to the log view and scroll it into view."""
        self._log_buf.insert(self._log_buf.get_end_iter(), text.rstrip("\n") + "\n")
        self._log_view.scroll_to_mark(self._log_end, 0.0, False, 0.0, 1.0)

    def _alert(self, heading: str, body: str) -> None:
        """Present a simple modal alert dialog."""
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def _on_ocr_toggled(self, *_args: object) -> None:
        """Enable the language entry only while OCR is on."""
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
            self._folder_row.set_subtitle(self._folder)

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


class ScanMoleApp(Adw.Application):
    """The libadwaita application owning a single :class:`MainWindow`."""

    def __init__(self) -> None:
        """Register the activation handler."""
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app: Adw.Application) -> None:
        """Present the main window, creating it on first activation."""
        win = self.props.active_window or MainWindow(application=app)
        win.present()


def main(argv: list[str] | None = None) -> int:
    """Run the ScanMole GUI and return the application exit code."""
    GLib.set_application_name("ScanMole")
    app = ScanMoleApp()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
