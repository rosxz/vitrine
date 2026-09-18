"""Global settings: theme selection and Steam source options.

Opened from the cog in the window's corner as a movable top-level window (the
project default is that editors and settings live in real windows you can move
around). Store-level flags like global launch defaults will live here too as
the app grows.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..library import Library
from ..sources.steam_source import FAMILY_SETTING
from .theme import THEMES, ThemeManager

STEAM_SOURCE_NAME = "Steam"


class SettingsWindow(Gtk.Window):
    def __init__(
        self,
        library: Library,
        theme_manager: ThemeManager,
        on_theme: Callable[[str], None] | None = None,
        on_steam_login: Callable[[], None] | None = None,
        on_steam_refresh: Callable[[], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Settings")
        self.library = library
        self.theme_manager = theme_manager
        self._on_theme = on_theme
        self._on_steam_login = on_steam_login or (lambda: None)
        self._on_steam_refresh = on_steam_refresh or (lambda: None)
        self.add_css_class("vitrine-window")
        self.set_default_size(460, 460)
        if parent is not None:
            self.set_transient_for(parent)

        page = Adw.PreferencesPage()
        page.add(self._build_appearance_group())
        page.add(self._build_steam_group())

        style = Gtk.ScrolledWindow()
        style.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        style.set_child(page)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings", subtitle=""))
        header.set_show_end_title_buttons(True)

        # Make the header the window's single title bar (with native window
        # buttons) rather than a second header inside the content, which would
        # duplicate the Close affordance.
        self.set_titlebar(header)
        self.set_child(style)

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Appearance")

        theme_row = Adw.ComboRow(title="Theme")
        theme_list = Gtk.StringList.new([t.name for t in THEMES.values()])
        theme_row.set_model(theme_list)
        theme_ids = list(THEMES)
        current = self.theme_manager.theme
        theme_row.set_selected(theme_ids.index(current) if current in theme_ids else 0)
        theme_row.connect("notify::selected-item", self._on_theme_selected)
        group.add(theme_row)
        return group

    def _build_steam_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=STEAM_SOURCE_NAME)

        family_row = Adw.SwitchRow(title="Include Steam Family library")
        family_row.set_subtitle("Also show games shared with your Steam Family group")
        family_row.set_active(bool(self.library.setting(FAMILY_SETTING, True)))
        family_row.connect("notify::active", self._on_family_toggled)
        group.add(family_row)

        login_button = Gtk.Button(label="Sign in / refresh Steam")
        login_button.connect("clicked", lambda _b: self._on_steam_login())
        login_button.set_halign(Gtk.Align.FILL)
        login_button.set_margin_top(6)
        group.add(_row_widget(login_button))
        return group

    # -- behaviour ------------------------------------------------------------

    def _on_theme_selected(self, row: Gtk.ComboRow, _pspec: object) -> None:
        index = row.get_selected()
        ids = list(THEMES)
        if index < 0 or index >= len(ids):
            return
        self.theme_manager.apply(ids[index])
        self.library.set_setting("theme", ids[index])
        if self._on_theme is not None:
            self._on_theme(ids[index])

    def _on_family_toggled(self, row: Gtk.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(FAMILY_SETTING, row.get_active())


def _row_widget(widget: Gtk.Widget) -> Gtk.Widget:
    """Wrap a plain button so it looks like a preference row."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_margin_top(4)
    box.set_margin_bottom(4)
    box.set_margin_start(16)
    box.set_margin_end(16)
    box.append(widget)
    return box


# Backwards-compatible alias.
SettingsDialog = SettingsWindow