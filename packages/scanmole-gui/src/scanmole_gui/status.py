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


def render_session_update(
    state: SessionState,
    update: Update,
    set_running_bar: Callable[[str], None],
    append_log: Callable[[str], None],
) -> None:
    """Render one session update into translated running-state text."""
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
        self._expander = Gtk.Expander(
            label=_("Log"), hexpand=True, valign=Gtk.Align.CENTER
        )
        log_header.append(self._expander)
        copy_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        copy_btn.set_child(
            Adw.ButtonContent(icon_name="edit-copy-symbolic", label=_("Copy"))
        )
        copy_btn.add_css_class("flat")
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
        self, on_show: Callable[[], None], on_open: Callable[[], None]
    ) -> None:
        """Build the bar; ``on_show``/``on_open`` act on the finished PDF."""
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

    def set_state(
        self, state: str, title: str, detail: str = "", *, actions: bool = False
    ) -> None:
        """Put the bar into ``idle``/``running``/``success``/``error``.

        ``actions`` shows the Show/Open buttons; the window decides it,
        because only the window knows whether an output file exists.
        """
        self._title.set_text(title)
        self._detail.set_text(detail)
        self._detail.set_visible(bool(detail))
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
