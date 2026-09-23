"""Global settings: theme selection and Steam source options.

Opened from the cog in the window's corner as a movable top-level window (the
project default is that editors and settings live in real windows you can move
around). Store-level flags like global launch defaults will live here too as
the app grows.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from .. import artwork
from ..artwork_providers import PROVIDER_IDS
from ..artwork_providers.base import provider_label
from ..library import (
    DEBUG_LOG_SETTING,
    DUMP_LAUNCH_ENV_SETTING,
    SHOW_DETAIL_SETTING,
    SHOW_HIDDEN,
    Library,
)
from ..sources.steam_source import FAMILY_SETTING
from .theme import THEMES, ThemeManager


class SettingsWindow(Gtk.Window):
    def __init__(
        self,
        library: Library,
        theme_manager: ThemeManager,
        on_theme: Callable[[str], None] | None = None,
        on_steam_login: Callable[[], None] | None = None,
        on_steam_refresh: Callable[[], None] | None = None,
        on_steam_reset: Callable[[], None] | None = None,
        on_gog_login: Callable[[], None] | None = None,
        on_gog_refresh: Callable[[], None] | None = None,
        on_gog_reset: Callable[[], None] | None = None,
        on_epic_login: Callable[[], None] | None = None,
        on_epic_refresh: Callable[[], None] | None = None,
        on_epic_reset: Callable[[], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Settings")
        self.library = library
        self.theme_manager = theme_manager
        self._on_theme = on_theme
        self._on_steam_login = on_steam_login or (lambda: None)
        self._on_steam_refresh = on_steam_refresh or (lambda: None)
        self._on_steam_reset = on_steam_reset or (lambda: None)
        self._on_gog_login = on_gog_login or (lambda: None)
        self._on_gog_refresh = on_gog_refresh or (lambda: None)
        self._on_gog_reset = on_gog_reset or (lambda: None)
        self._on_epic_login = on_epic_login or (lambda: None)
        self._on_epic_refresh = on_epic_refresh or (lambda: None)
        self._on_epic_reset = on_epic_reset or (lambda: None)
        self._secret_rows: list[tuple[Adw.PasswordEntryRow, str]] = []
        self.add_css_class("vitrine-window")
        self.set_default_size(460, 460)
        if parent is not None:
            self.set_transient_for(parent)

        self.connect("close-request", self._on_close, None)

        notebook = Gtk.Notebook()
        notebook.set_scrollable(True)
        notebook.set_vexpand(True)
        for title, page in (
            ("General", self._build_general_page()),
            ("Appearance", self._build_appearance_page()),
            ("Providers", self._build_providers_page()),
        ):
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(page)
            notebook.append_page(scroller, Gtk.Label(label=title))

        self._overlay = Adw.ToastOverlay()
        self._overlay.set_child(notebook)
        content = self._overlay

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Settings", subtitle=""))
        header.set_show_end_title_buttons(True)

        # Make the header the window's single title bar (with native window
        # buttons) rather than a second header inside the content, which would
        # duplicate the Close affordance.
        self.set_titlebar(header)
        self.set_child(content)

    def _build_general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="General")
        debug_row = Adw.SwitchRow(title="Show debug log window")
        debug_row.set_subtitle("Open the live log automatically for installs and launches (all sources)")
        debug_row.set_active(bool(self.library.setting(DEBUG_LOG_SETTING, False)))
        debug_row.connect("notify::active", self._on_debug_toggled)
        group.add(debug_row)

        dump_row = Adw.SwitchRow(title="Dump launch environment to file")
        dump_row.set_subtitle(
            "On each Proton launch, write the exact command and environment to "
            "`proton-launch.env` for diagnosing window-presentation issues. "
            "Useful while debugging but unnecessary day to day."
        )
        dump_row.set_active(bool(self.library.setting(DUMP_LAUNCH_ENV_SETTING, False)))
        dump_row.connect("notify::active", self._on_dump_env_toggled)
        group.add(dump_row)

        hidden_row = Adw.SwitchRow(title="Show hidden / blacklisted games")
        hidden_row.set_subtitle("Reveal games you hid from the library, so you can unhide them")
        hidden_row.set_active(bool(self.library.setting(SHOW_HIDDEN, False)))
        hidden_row.connect("notify::active", self._on_show_hidden_toggled)
        group.add(hidden_row)

        detail_row = Adw.SwitchRow(title="Show game description bar")
        detail_row.set_subtitle("Show the selected game's hero/description panel at the top")
        detail_row.set_active(bool(self.library.setting(SHOW_DETAIL_SETTING, True)))
        detail_row.connect("notify::active", self._on_show_detail_toggled)
        group.add(detail_row)
        page.add(group)
        return page

    def _on_show_hidden_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(SHOW_HIDDEN, row.get_active())

    def _on_show_detail_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(SHOW_DETAIL_SETTING, row.get_active())

    def _build_appearance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()

        appearance_group = Adw.PreferencesGroup(title="Appearance")
        theme_row = Adw.ComboRow(title="Theme")
        theme_list = Gtk.StringList.new([t.name for t in THEMES.values()])
        theme_row.set_model(theme_list)
        theme_ids = list(THEMES)
        current = self.theme_manager.theme
        theme_row.set_selected(theme_ids.index(current) if current in theme_ids else 0)
        theme_row.connect("notify::selected-item", self._on_theme_selected)
        appearance_group.add(theme_row)
        page.add(appearance_group)

        page.add(self._build_artwork_group())

        refresh_group = Adw.PreferencesGroup(title="Artwork refresh")
        refresh_row = Adw.SwitchRow(title="Refresh artwork for all games")
        refresh_row.set_subtitle(
            "When refreshing the library, also re-pull artwork for games that "
            "already have it. Off by default: only games missing artwork are "
            "fetched, so it never overwrites your choices."
        )
        refresh_row.set_active(bool(self.library.setting(artwork.FORCE_REFRESH_SETTING, False)))
        refresh_row.connect("notify::active", self._on_force_refresh_toggled)
        refresh_group.add(refresh_row)
        page.add(refresh_group)
        return page

    def _on_force_refresh_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(artwork.FORCE_REFRESH_SETTING, row.get_active())

    def _build_providers_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.add(self._build_provider_group())
        return page

    def _build_provider_group(self) -> Adw.PreferencesGroup:
        """Accounts for every game provider (Steam, GOG, Epic) under one umbrella."""
        group = Adw.PreferencesGroup(
            title="Provider",
            description="Sign in to each game store whose library you want Vitrine "
            "to see. Each provider works the same way; pick the stores you use.",
        )

        family_row = Adw.SwitchRow(title="Include Steam Family library")
        family_row.set_subtitle("Also show games shared with your Steam Family group")
        family_row.set_active(bool(self.library.setting(FAMILY_SETTING, True)))
        family_row.connect("notify::active", self._on_family_toggled)
        group.add(family_row)

        group.add(self._source_section(
            "Steam",
            "Linked, owned Steam games appear in your library.",
            self._on_steam_login,
            self._on_steam_reset,
        ))
        group.add(self._source_section(
            "GOG",
            "Link your GOG account to browse owned games and install them.",
            self._on_gog_login,
            self._on_gog_reset,
        ))
        hint = "Requires the 'legendary' CLI on PATH. See the README."
        group.add(self._source_section(
            "Epic",
            "Link your Epic Games account to browse owned games and install them.",
            self._on_epic_login,
            self._on_epic_reset,
            hint=hint,
        ))
        return group

    def _source_section(
        self,
        name: str,
        subtitle: str,
        on_login: Callable[[], None],
        on_reset: Callable[[], None],
        hint: str | None = None,
    ) -> Gtk.Widget:
        """Render one provider's account actions as a labelled block."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(14)

        title = Gtk.Label(label=name, halign=Gtk.Align.START)
        title.add_css_class("title-4")
        box.append(title)
        if subtitle:
            desc = Gtk.Label(label=subtitle, wrap=True, xalign=0.0)
            desc.add_css_class("dim-label")
            box.append(desc)

        login = Gtk.Button(label=f"Sign in / refresh {name}")
        login.connect("clicked", lambda _b: on_login())
        login.set_halign(Gtk.Align.FILL)
        login.set_margin_top(8)
        box.append(login)

        reset = Gtk.Button(label=f"Reset {name} session…")
        reset.set_tooltip_text("Clear saved login, then sign in again")
        reset.add_css_class("destructive-action")
        reset.set_halign(Gtk.Align.FILL)
        reset.set_margin_top(6)
        reset.connect("clicked", lambda _b: on_reset())
        box.append(reset)

        if hint:
            hint_label = Gtk.Label(label=hint, wrap=True, xalign=0.0)
            hint_label.add_css_class("dim-label")
            hint_label.set_margin_top(4)
            box.append(hint_label)
        return box

    def _on_dump_env_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(DUMP_LAUNCH_ENV_SETTING, row.get_active())

    def _on_debug_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.library.set_setting(DEBUG_LOG_SETTING, row.get_active())

    # -- artwork providers -----------------------------------------------------

    def _build_artwork_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Artwork providers",
            description="Set which services provide tile/hero art. IGDB needs Twitch "
            "credentials; SteamGridDB needs its API key. Environment overrides "
            "(VITRINE_IGDB_CLIENT_ID/-SECRET, VITRINE_STEAMGRIDDB_KEY) take precedence.",
        )

        group.add(self._secret_row("IGDB Client-ID", artwork.IGDB_CLIENT_ID_SETTING))
        group.add(self._secret_row("IGDB Client-Secret", artwork.IGDB_CLIENT_SECRET_SETTING))
        group.add(self._secret_row("SteamGridDB API key", artwork.STEAMGRIDDB_KEY_SETTING))

        # Provider priority per slot, reorderable. Order listed top → bottom = highest
        # → lowest priority; the first provider that has art for the slot is used.
        tile_priority, hero_priority = artwork.load_priority(self.library)
        group.add(_heading(
            "Tile cover priority",
            "Order: top = highest priority. The first provider that has a "
            "portrait cover is used for the tile slot.",
        ))
        group.add(self._priority_editor("tile", artwork.TILE_PRIORITY_SETTING, tile_priority))
        group.add(_heading(
            "Hero banner priority",
            "Order: top = highest priority. The first provider that has a wide "
            "banner is used for the hero slot.",
        ))
        group.add(self._priority_editor("hero", artwork.HERO_PRIORITY_SETTING, hero_priority))

        # Final resolution of cached artwork (capped by the source image).
        group.add(self._dim_row("Tile cover", artwork.TILE_DIM_SETTING, default=640))
        group.add(self._dim_row("Hero banner", artwork.HERO_DIM_SETTING, default=1280))

        test = Gtk.Button(label="Test connections")
        test.set_tooltip_text("Check which configured providers can be reached")
        test.connect("clicked", self._on_test_providers)
        test.set_halign(Gtk.Align.FILL)
        group.add(_row_widget(test))
        return group

    def _secret_row(self, title: str, setting_key: str) -> Gtk.Widget:
        row = Adw.PasswordEntryRow(title=title)
        row.set_text(str(self.library.setting(setting_key, "") or ""))
        row.connect("apply", self._on_secret_applied, setting_key)
        self._secret_rows.append((row, setting_key))
        return row

    def _on_secret_applied(self, row: Adw.PasswordEntryRow, setting_key: str) -> None:
        self.library.set_setting(setting_key, row.get_text())

    def _dim_row(self, title: str, setting_key: str, default: int) -> Gtk.Widget:
        stored = self.library.setting(setting_key)
        value = int(stored) if stored else default
        adjustment = Gtk.Adjustment(value=value, lower=128, upper=16384, step_increment=128)
        row = Adw.SpinRow(adjustment=adjustment, climb_rate=0.5, digits=0, title=title)
        row.add_suffix(Gtk.Label(label="px"))
        row.connect("notify::value", self._on_dim_changed, setting_key)
        return row

    def _on_dim_changed(self, row: Adw.SpinRow, _pspec: object, setting_key: str) -> None:
        self.library.set_setting(setting_key, int(row.get_value()))

    def _flush_secrets(self) -> None:
        """Persist every secret row's current text, then make them match the DB.

        ``apply`` only fires on Enter, so we also save on Test and on window
        close to avoid losing a typed-but-not-Entered value.
        """
        for row, setting_key in self._secret_rows:
            self.library.set_setting(setting_key, row.get_text())

    def _on_close(self, *_args) -> bool:
        self._flush_secrets()
        return False  # allow the window to close

    def _priority_editor(self, slot: str, setting_key: str, initial: list[str]) -> Gtk.Widget:
        order = [p for p in initial if p in PROVIDER_IDS]
        for pid in PROVIDER_IDS:
            if pid not in order:
                order.append(pid)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        def rebuild() -> None:
            for row in list(listbox):
                listbox.remove(row)
            for i, pid in enumerate(order):
                listbox.append(_priority_row(pid, i, len(order), on_up, on_down))
            self.library.set_setting(setting_key, order)

        def on_up(pid: str) -> None:
            idx = order.index(pid)
            if idx > 0:
                order[idx], order[idx - 1] = order[idx - 1], order[idx]
                rebuild()

        def on_down(pid: str) -> None:
            idx = order.index(pid)
            if idx < len(order) - 1:
                order[idx], order[idx + 1] = order[idx + 1], order[idx]
                rebuild()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(listbox)
        rebuild()
        return _row_widget(box)

    def _on_test_providers(self, _button: Gtk.Button) -> None:
        from ..artwork_providers import igdb as igdb_mod
        from ..artwork_providers import steamgriddb as sgdb_mod

        self._flush_secrets()  # save typed-but-not-Entered credentials first
        creds = artwork.load_credentials(self.library)

        def test_igdb() -> str:
            if not artwork._igdb_configured(creds):
                return "IGDB: no credentials"
            return "IGDB: ok" if igdb_mod.probe(creds["igdb_client_id"], creds["igdb_client_secret"]) \
                else "IGDB: auth failed"

        def test_steamgriddb() -> str:
            if not artwork._steamgriddb_configured(creds):
                return "SteamGridDB: no API key"
            return "SteamGridDB: ok" if sgdb_mod.probe(creds["steamgriddb_key"]) \
                else "SteamGridDB: request failed"

        def _worker() -> None:
            lines = [test_igdb(), test_steamgriddb()]
            GLib_idle(lambda: self._show_test_results("\n".join(lines)))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_test_results(self, message: str) -> None:
        self._toast(message)

    def _toast(self, message: str) -> None:
        self._overlay.add_toast(Adw.Toast(title=message))

    

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


def GLib_idle(fn: Callable[[], None], *args) -> None:
    """Schedule ``fn(*args)`` on the GTK main loop."""
    if args:
        GLib.idle_add(lambda: fn(*args))
    else:
        GLib.idle_add(fn)


def _heading(title: str, subtitle: str) -> Gtk.Widget:
    """A section heading rendered above its list (not as a preference row)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    label = Gtk.Label(label=title, halign=Gtk.Align.START)
    label.add_css_class("caption")
    label.add_css_class("bold")
    box.append(label)
    sub = Gtk.Label(label=subtitle, wrap=True, xalign=0.0)
    sub.add_css_class("dim-label")
    box.append(sub)
    return _row_widget(box)


def _priority_row(
    provider: str,
    index: int,
    count: int,
    on_up,  # noqa: ANN001
    on_down,  # noqa: ANN001
) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    label = Gtk.Label(label=provider_label(provider), hexpand=True, halign=Gtk.Align.START)
    row.append(label)
    up = Gtk.Button(label="↑")
    up.connect("clicked", lambda _b: on_up(provider))
    up.set_sensitive(index > 0)
    down = Gtk.Button(label="↓")
    down.connect("clicked", lambda _b: on_down(provider))
    down.set_sensitive(index < count - 1)
    row.append(up)
    row.append(down)
    return row


# Backwards-compatible alias.
SettingsDialog = SettingsWindow