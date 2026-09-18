"""Global settings: theme selection.

Opened from the cog in the window's corner as a movable top-level window (the
project default is that editors and settings live in real windows you can move
around). Store-level flags like global launch defaults will live here too as
the app grows.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..library import Library
from .theme import THEMES, ThemeManager


class SettingsWindow(Gtk.Window):
    def __init__(
        self,
        library: Library,
        theme_manager: ThemeManager,
        on_theme: Callable[[str], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Settings")
        self.library = library
        self.theme_manager = theme_manager
        self._on_theme = on_theme
        self.set_default_size(460, 420)
        if parent is not None:
            self.set_transient_for(parent)

        group = Adw.PreferencesGroup(title="Appearance")

        theme_row = Adw.ComboRow(title="Theme")
        theme_list = Gtk.StringList.new([t.name for t in THEMES.values()])
        theme_row.set_model(theme_list)
        theme_ids = list(THEMES)
        current = self.theme_manager.theme
        theme_row.set_selected(theme_ids.index(current) if current in theme_ids else 0)
        theme_row.connect("notify::selected-item", self._on_theme_selected)
        group.add(theme_row)

        page = Adw.PreferencesPage()
        page.add(group)

        style = Gtk.ScrolledWindow()
        style.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        style.set_child(page)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header_bar())
        toolbar.set_content(style)

        self.set_child(toolbar)

    def _build_header_bar(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings", subtitle=""))
        header.set_show_end_title_buttons(True)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _btn: self.close())
        header.pack_end(close)
        return header

    def _on_theme_selected(self, row: Gtk.ComboRow, _pspec: object) -> None:
        index = row.get_selected()
        ids = list(THEMES)
        if index < 0 or index >= len(ids):
            return
        self.theme_manager.apply(ids[index])
        self.library.set_setting("theme", ids[index])
        if self._on_theme is not None:
            self._on_theme(ids[index])


# Backwards-compatible alias.
SettingsDialog = SettingsWindow