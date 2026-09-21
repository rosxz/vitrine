"""A live execution/install log window.

Buffers the streamed output of a command (e.g. ``legendary install``) and shows
it in a scrollable text view. A simple append-only throughput avoids re-setting
the entire buffer on every line for large outputs.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from gi.repository import Adw, GObject, Gtk

from ..downloads import DownloadJob


class ExecutionLogWindow(Gtk.Window):
    """A movable window that tails a command's output live."""

    def __init__(
        self,
        title: str,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title=title)
        self.set_default_size(720, 480)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        self._buffer = Gtk.TextBuffer()
        self._buffer.create_tag("err", foreground="#e05656")
        self._view = Gtk.TextView(buffer=self._buffer)
        self._view.set_editable(False)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._view.set_cursor_visible(False)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._view)
        scroller.set_vexpand(True)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=""))
        header.set_show_end_title_buttons(True)
        close = Gtk.Button(label="Close")
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda _b: self.close())
        header.pack_start(close)

        self.set_titlebar(header)
        self.set_child(scroller)

        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._idle_armed = False

    # -- appending output -----------------------------------------------------

    def append_line(self, line: str) -> None:
        """Queue a line for display; safe to call from any thread."""
        with self._lock:
            self._pending.append(line)
            if not self._idle_armed:
                self._idle_armed = True
                GLib_idle(self._drain)

    def _drain(self) -> None:
        with self._lock:
            lines = self._pending
            self._pending = []
            self._idle_armed = False
        if lines:
            end = self._buffer.get_end_iter()
            for line in lines:
                self._buffer.insert(end, line + "\n")
            self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        end = self._buffer.get_end_iter()
        self._buffer.place_cursor(end)
        mark = self._buffer.create_mark(None, end, False)
        self._view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def on_empty(self) -> None:
        self._buffer.insert(self._buffer.get_start_iter(), "(no output yet)\n")


def GLib_idle(fn: Callable[[], None]) -> None:
    from gi.repository import GLib

    GLib.idle_add(fn)


def stream_job_to_window(job: DownloadJob, window: ExecutionLogWindow) -> Callable[[str], None]:
    """Wire a download job's output into an ExecutionLogWindow.

    Returns a callback suitable for passing as the job's ``on_line``.
    """

    def _on_line(line: str) -> None:
        window.append_line(line)

    return _on_line


GObject.type_register(ExecutionLogWindow)