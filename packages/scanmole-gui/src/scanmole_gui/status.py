"""Scan status rendering: the log pane and the persistent result bar.

Focused GTK views plus the translation of session updates and exit
codes into user-facing text. Decisions stay elsewhere: completion,
cancellation precedence, runner identity and side effects belong to
the window and the GTK-free session module; these components only
render what they are told.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402  # after require_version

from scanmole_gui.i18n import _, ngettext  # noqa: E402  # after gi setup
from scanmole_gui.session import SessionState, Update  # noqa: E402

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


def exit_failure_texts(exit_code: int, error_message: str | None) -> tuple[str, str]:
    """The alert heading and body for a failed scan exit."""
    heading, body = EXIT_HINTS.get(
        exit_code,
        (
            _("Scan Failed"),
            _("scanmole exited with status %(code)d. See the log for details.")
            % {"code": exit_code},
        ),
    )
    if error_message:
        body = body + "\n\n" + _("Details:") + " " + error_message
    return heading, body


def success_summary(pages: int, blanks: int) -> str:
    """The result-bar text for a successful scan."""
    summary = ngettext("%d page saved", "%d pages saved", pages) % pages
    if blanks:
        summary += " \u00b7 " + (
            ngettext("%d blank skipped", "%d blanks skipped", blanks) % blanks
        )
    return summary


def waiting_text(sheets: int, manual: bool) -> str:
    """The result-bar text for a collect run waiting between sheets.

    Always phrased in physical sheets (the engine counts them with duplex
    grouping); a duplex side count must never be presented as sheets.
    """
    if manual:
        if not sheets:
            return _("Place the first sheet, then press Next Sheet.")
        return ngettext(
            "%(count)d sheet scanned. Place the next sheet, then press Next Sheet.",
            "%(count)d sheets scanned. Place the next sheet, then press Next Sheet.",
            sheets,
        ) % {"count": sheets}
    if not sheets:
        return _("Insert the first sheet.")
    return ngettext(
        "%(count)d sheet scanned. Insert the next sheet.",
        "%(count)d sheets scanned. Insert the next sheet.",
        sheets,
    ) % {"count": sheets}


def close_confirmation_text(state: SessionState) -> tuple[str, str]:
    """The heading and body for closing on top of captured pages.

    Counted in sheets while a collect run waits (the engine grouped them
    for us) and in pages otherwise, so the number always matches what the
    result bar was showing a moment ago. The body names the action that
    saves the work rather than only stating that it would be lost, and
    only offers Finish where that button actually exists.
    """
    if state.waiting:
        heading = (
            ngettext(
                "Close and discard %d scanned sheet?",
                "Close and discard %d scanned sheets?",
                state.waiting_sheets,
            )
            % state.waiting_sheets
        )
        body = _(
            "No PDF has been created yet. Press Finish in the status bar "
            "to save them first."
        )
    else:
        heading = (
            ngettext(
                "Close and discard %d scanned page?",
                "Close and discard %d scanned pages?",
                state.pages,
            )
            % state.pages
        )
        body = _("The scan is still running and no PDF has been created yet.")
    return heading, body


def render_session_update(
    state: SessionState,
    update: Update,
    set_running_bar: Callable[[str], None],
    append_log: Callable[[str], None],
    set_waiting_bar: Callable[[str, bool], None] | None = None,
) -> None:
    """Render one session update into translated running-state text.

    ``set_waiting_bar`` receives the waiting text plus whether continuing
    needs a manual trigger (which shows the Next Sheet action); without
    it the waiting text goes through ``set_running_bar``.
    """
    if update is Update.STARTED:
        set_running_bar(_("Scanning\u2026"))
    elif update is Update.PAGE:
        text = _("Page %d scanned") % state.pages
        if state.blanks:
            text += (
                ngettext(" (%d blank skipped)", " (%d blanks skipped)", state.blanks)
                % state.blanks
            )
        set_running_bar(text + "\u2026")
    elif update is Update.SCAN_DONE:
        total = state.total or 0
        kept = state.kept or 0
        set_running_bar(
            ngettext(
                "Scan finished \u2014 keeping %(kept)d of %(total)d page\u2026",
                "Scan finished \u2014 keeping %(kept)d of %(total)d pages\u2026",
                total,
            )
            % {"kept": kept, "total": total}
        )
    elif update is Update.WAITING:
        text = waiting_text(state.waiting_sheets, state.waiting_manual)
        if set_waiting_bar is not None:
            set_waiting_bar(text, state.waiting_manual)
        else:
            set_running_bar(text)
    elif update is Update.OCR_STARTED:
        set_running_bar(_("Running OCR\u2026"))
    elif update is Update.ERROR:
        message = state.error_message or _("Unknown error")
        append_log(f"[error] {message}")


class LogView:
    """The collapsed, copyable log below the form."""

    def __init__(self) -> None:
        """Build the header (expander, copy) and the hidden text pane."""
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._expander = Gtk.Expander(label=_("Log"), valign=Gtk.Align.CENTER)
        log_header.append(self._expander)
        # Both name the same pane, so they sit together at the left rather
        # than at opposite ends of the row. .log-copy takes the button's
        # bold label down to the weight the expander beside it uses.
        copy_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        copy_btn.set_child(
            Adw.ButtonContent(icon_name="edit-copy-symbolic", label=_("Copy"))
        )
        copy_btn.add_css_class("flat")
        copy_btn.add_css_class("log-copy")
        copy_btn.connect("clicked", self._on_copy)
        log_header.append(copy_btn)
        self.widget.append(log_header)
        log_scroller = Gtk.ScrolledWindow(
            min_content_height=210, has_frame=True, visible=False
        )
        self._view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            left_margin=6,
            right_margin=6,
            top_margin=4,
            bottom_margin=4,
        )
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._buffer = self._view.get_buffer()
        self._end_mark = self._buffer.create_mark(
            None, self._buffer.get_end_iter(), False
        )
        log_scroller.set_child(self._view)
        self._expander.connect(
            "notify::expanded",
            lambda *_a: log_scroller.set_visible(self._expander.get_expanded()),
        )
        self.widget.append(log_scroller)

    def append(self, text: str) -> None:
        """Append a line to the log view and scroll it into view."""
        self._buffer.insert(self._buffer.get_end_iter(), text.rstrip("\n") + "\n")
        self._view.scroll_to_mark(self._end_mark, 0.0, False, 0.0, 1.0)

    def _on_copy(self, *_args: object) -> None:
        """Copy the whole log text to the clipboard."""
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, True)
        provider = Gdk.ContentProvider.new_for_value(text)
        self._view.get_clipboard().set_content(provider)


class ResultBar:
    """The persistent bottom bar showing progress and the result."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_open: Callable[[], None],
        on_next_sheet: Callable[[], None] = lambda: None,
        on_finish: Callable[[], None] = lambda: None,
    ) -> None:
        """Build the bar; ``on_show``/``on_open`` act on the finished PDF,
        ``on_next_sheet``/``on_finish`` drive a waiting collect run."""
        self._on_next_sheet = on_next_sheet
        self._on_finish = on_finish
        # Centered as a whole: with mixed icon, two-line text and buttons a
        # left-aligned bar never lines up optically with the groups above.
        self.widget = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self._spinner = Gtk.Spinner(visible=False)
        self.widget.append(self._spinner)
        self._icon = Gtk.Image(icon_name="object-select-symbolic", visible=False)
        self.widget.append(self._icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self._title = Gtk.Label(xalign=0.0, label=_("Ready."))
        self._title.add_css_class("heading")
        self._title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        labels.append(self._title)
        self._detail = Gtk.Label(xalign=0.0, visible=False)
        self._detail.add_css_class("caption")
        self._detail.add_css_class("dim-label")
        self._detail.add_css_class("monospace")
        self._detail.set_ellipsize(3)
        labels.append(self._detail)
        # A separate label, because the detail above is shaped for a file
        # name (monospace, ellipsized) and a sentence there would be cut
        # off mid-word.
        self._note = Gtk.Label(xalign=0.0, visible=False, wrap=True)
        self._note.add_css_class("caption")
        self._note.add_css_class("dim-label")
        labels.append(self._note)
        self.widget.append(labels)
        self._show_btn = Gtk.Button(visible=False)
        self._show_btn.set_child(
            Adw.ButtonContent(icon_name="folder-open-symbolic", label=_("Show"))
        )
        self._show_btn.connect("clicked", lambda *_a: on_show())
        self.widget.append(self._show_btn)
        self._open_btn = Gtk.Button(visible=False)
        self._open_btn.set_child(
            Adw.ButtonContent(icon_name="x-office-document-symbolic", label=_("Open"))
        )
        self._open_btn.connect("clicked", lambda *_a: on_open())
        self.widget.append(self._open_btn)
        # Collect-wait actions; hidden unless show_wait_actions() puts them
        # up, and every set_state() clears them again.
        self._next_btn = Gtk.Button(visible=False)
        self._next_btn.set_child(
            Adw.ButtonContent(icon_name="go-next-symbolic", label=_("Next Sheet"))
        )
        self._next_btn.connect("clicked", self._on_next_clicked)
        self.widget.append(self._next_btn)
        self._finish_btn = Gtk.Button(visible=False)
        self._finish_btn.set_child(
            Adw.ButtonContent(icon_name="object-select-symbolic", label=_("Finish"))
        )
        self._finish_btn.add_css_class("suggested-action")
        self._finish_btn.connect("clicked", self._on_finish_clicked)
        self.widget.append(self._finish_btn)

    def _on_next_clicked(self, *_args: object) -> None:
        """Request the next sheet; disabled until the next state update."""
        self._next_btn.set_sensitive(False)
        self._on_next_sheet()

    def _on_finish_clicked(self, *_args: object) -> None:
        """Request the finish; both actions lock until the run reacts."""
        self._next_btn.set_sensitive(False)
        self._finish_btn.set_sensitive(False)
        self._on_finish()

    def show_wait_actions(self, *, next_sheet: bool) -> None:
        """Show Finish (plus Next Sheet for manual flows), re-enabled."""
        self._next_btn.set_visible(next_sheet)
        self._next_btn.set_sensitive(True)
        self._finish_btn.set_visible(True)
        self._finish_btn.set_sensitive(True)

    def set_state(
        self,
        state: str,
        title: str,
        detail: str = "",
        *,
        actions: bool = False,
        note: str = "",
    ) -> None:
        """Put the bar into ``idle``/``running``/``success``/``error``.

        ``actions`` shows the Show/Open buttons; the window decides it,
        because only the window knows whether an output file exists.
        ``note`` carries one advisory sentence about the finished result,
        and is cleared by every state that does not repeat it.
        """
        self._title.set_text(title)
        self._detail.set_text(detail)
        self._detail.set_visible(bool(detail))
        self._note.set_text(note)
        self._note.set_visible(bool(note))
        running = state == "running"
        self._spinner.set_visible(running)
        if running:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._icon.set_visible(state in ("success", "error"))
        self._icon.set_from_icon_name(
            "dialog-error-symbolic" if state == "error" else "object-select-symbolic"
        )
        if state == "success":
            self._icon.add_css_class("success")
        else:
            self._icon.remove_css_class("success")
        self._show_btn.set_visible(actions)
        self._open_btn.set_visible(actions)
        self._next_btn.set_visible(False)
        self._finish_btn.set_visible(False)
