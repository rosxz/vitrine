"""Main window: source sidebar on the left, library grid + detail bar on the right."""

from __future__ import annotations

import glob
import logging
from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..library import Game, Library
from ..paths import secret_dir
from ..running import GameAlreadyRunning, Runtime
from ..sources import registry
from ..sources.steam.auth import SteamTokenStore
from ..sources.steam_source import SteamAuthError, SteamSource
from .game_detail_bar import GameDetailBar
from .game_dialogs import AddGameDialog, GameSettingsDialog
from .library_view import LibraryView
from .settings_dialog import SettingsDialog
from .steam_login_dialog import SteamLoginDialog

logger = logging.getLogger(__name__)

ALL_GAMES = "__all__"


class VitrineWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, library: Library) -> None:
        super().__init__(application=application, title="Vitrine")
        self.library = library
        self.theme_manager = application.theme_manager
        self.current_source: str | None = None

        self.set_default_size(1100, 760)
        self.add_css_class("vitrine-window")

        self.runtime = Runtime()
        self.runtime.on_start = self._on_game_started
        self.runtime.on_exit = self._on_game_exited

        self.library_view = LibraryView(on_activate=self.on_game_activated, on_context=self.on_tile_context)
        self.library_view.connect("selection-changed", self._on_selection_changed)

        self.detail_bar = GameDetailBar(
            on_play=self._on_detail_play,
            on_settings=self._on_detail_settings,
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self.library_view)
        content_box.append(self.detail_bar)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(content_box)
        self.toasts.set_hexpand(True)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Vitrine")
        header.set_title_widget(title)
        self.title_widget = title

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a game")
        add_button.connect("clicked", self.on_add_game_clicked)
        header.pack_end(add_button)
        self.add_button = add_button

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Refresh Steam library")
        refresh_button.connect("clicked", self.on_refresh_source)
        refresh_button.set_visible(False)
        header.pack_end(refresh_button)
        self.refresh_button = refresh_button

        cog = Gtk.Button(icon_name="emblem-system-symbolic")
        cog.set_tooltip_text("Settings")
        cog.connect("clicked", self.on_settings_clicked)
        header.pack_end(cog)
        self.cog_button = cog

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)

        split = Adw.OverlaySplitView()
        split.set_sidebar(self._build_sidebar())
        split.set_content(toolbar)
        # Slender, roughly 2/3 of the original 210-320px column.
        split.set_min_sidebar_width(140)
        split.set_max_sidebar_width(220)
        self.set_content(split)

        self._ticker: int | None = None
        self.setup_running_ticker()

        self.reload()

    # -- UI construction -------------------------------------------------------

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.add_css_class("sidebar")

        header = Gtk.Label(label="Sources", halign=Gtk.Align.START)
        header.add_css_class("sidebar-header")
        header.set_margin_start(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        sidebar.append(header)

        self.source_list = Gtk.ListBox()
        self.source_list.add_css_class("vitrine-nav")
        self.source_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.source_list.connect("row-selected", self.on_source_selected)

        self.source_rows: dict[str, Gtk.ListBoxRow] = {}
        self._add_source_row(ALL_GAMES, "All games", "view-grid-symbolic")
        for source in registry.all():
            self._add_source_row(source.id, source.name, source.icon or "application-x-executable-symbolic")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.source_list)
        scroller.set_vexpand(True)
        sidebar.append(scroller)

        return sidebar

    def _add_source_row(self, source_id: str, title: str, icon_name: str) -> None:
        row = Gtk.ListBoxRow()
        row.source_id = source_id  # type: ignore[attr-defined]
        row.add_css_class("vitrine-nav-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class("vitrine-nav-icon")
        box.append(icon)
        box.append(Gtk.Label(label=title, xalign=0))
        row.set_child(box)

        self.source_list.append(row)
        self.source_rows[source_id] = row
        if source_id == ALL_GAMES:
            self.source_list.select_row(row)

    # -- behaviour -------------------------------------------------------------

    def on_source_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        source_id = getattr(row, "source_id", ALL_GAMES)
        self.current_source = None if source_id == ALL_GAMES else source_id
        self.reload()

    def reload(self) -> None:
        games = self.library.games(source=self.current_source)
        self.library_view.set_games(games)
        self.title_widget.set_subtitle(
            "1 game" if len(games) == 1 else f"{len(games)} games"
        )
        # The Steam source gets a refresh button instead of "add a game".
        if self.current_source == "steam":
            self.add_button.set_visible(False)
            self.refresh_button.set_visible(True)
        else:
            self.add_button.set_visible(True)
            self.refresh_button.set_visible(False)

    def on_settings_clicked(self, _button: Gtk.Button) -> None:
        SettingsDialog(
            self.library,
            self.theme_manager,
            on_steam_login=self.on_steam_login,
            on_steam_refresh=self._run_steam_sync,
            on_steam_reset=self.on_steam_reset_session,
            parent=self,
        ).present()

    def on_add_game_clicked(self, _button: Gtk.Button) -> None:
        AddGameDialog(self.library, on_add=self.on_game_added, parent=self).present()

    def on_refresh_source(self, _button: Gtk.Button) -> None:
        self._run_steam_sync()

    def on_steam_login(self) -> None:
        """Open the Steam sign-in browser, then refresh the library."""
        source = SteamSource(self.library)

        def on_complete(ok: bool) -> None:
            if not ok:
                self.toasts.add_toast(Adw.Toast(title="Steam sign-in failed"))
                return
            self.toasts.add_toast(Adw.Toast(title="Steam sign-in complete"))
            self._run_steam_sync()

        store = source.login_token_store()
        if self.library.setting("steam_steamid"):
            # Reuse the account we already know about.
            store = type(store)(store.secret_dir, self.library.setting("steam_steamid"))
        dialog = SteamLoginDialog(store, on_complete=on_complete, parent=self)
        dialog.present()

    def on_steam_reset_session(self) -> None:
        """Clear stored Steam credentials so the user can sign in afresh."""
        cleared = 0
        for path in glob.glob(str(secret_dir() / "steam" / "auth_*.json")):
            steamid = path.rsplit("auth_", 1)[1].rsplit(".json", 1)[0]
            SteamTokenStore(secret_dir(), steamid).clear()
            cleared += 1
        # Drop every Steam library entry that is not actually installed on
        # disk, based on the app manifests (the DB's installed flag can be
        # stale for played-but-uninstalled games).
        on_disk = SteamSource(self.library).installed_on_disk()
        pruned = self.library.prune_source_games(
            "steam",
            keep_installed=False,
            preserve_on_disk=on_disk,
        )
        self.library.clear_source_games("steam")
        self.library.set_setting("steam_steamid", None)
        if self.current_source == "steam":
            self.current_source = None
        self.reload()
        self.toasts.add_toast(
            Adw.Toast(title="Steam session reset" if cleared else f"No credentials to reset · {pruned} removed")
        )

    def _run_steam_sync(self) -> None:
        try:
            source = SteamSource(self.library)
            if not source.is_authenticated():
                self.toasts.add_toast(Adw.Toast(title="Sign in to Steam first (cog → Steam)"))
                return
            # Record the account for future launches.
            if source.steamid64:
                self.library.set_setting("steam_steamid", source.steamid64)
            count = source.sync()
            source.sync_installed()
            self.current_source = "steam"
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"Steam refreshed · {count} games"))
        except SteamAuthError as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
        except Exception as error:  # noqa: BLE001
            logger.exception("Steam sync failed")
            self.toasts.add_toast(Adw.Toast(title=f"Steam sync failed: {error}"))

    def on_game_added(self, game: Game) -> None:
        self.library.add(game)
        self.reload()
        self.detail_bar.set_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Added {game.name}"))

    def _on_detail_play(self, game: Game | None) -> None:
        if game is not None:
            self.on_game_activated(game)

    def _on_detail_settings(self, game: Game | None) -> None:
        if game is not None:
            self.on_edit_game(game)

    def on_edit_game(self, game: Game) -> None:
        GameSettingsDialog(
            self.library,
            game,
            on_save=self.on_game_edited,
            on_remove=self.on_game_removed,
            parent=self,
        ).present()

    def on_game_edited(self, game: Game) -> None:
        self.reload()
        self.detail_bar.set_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Updated {game.name}"))

    def on_game_removed(self, game: Game) -> None:
        if game.source == "steam":
            self.library.remove_source_game("steam", game.source_id or "")
        else:
            self.library.remove(game.id) if game.id is not None else None
        self.reload()
        self.detail_bar.set_game(None)
        self.toasts.add_toast(Adw.Toast(title=f"Removed {game.name}"))

    def on_game_activated(self, game: Game) -> None:
        # An uninstalled store game links to its store page rather than launching.
        if game.source == "steam" and not game.installed:
            self.open_store_page(game)
            return
        # Installed Steam games launch through Steam itself, not the local
        # Wine/Proton pipeline.
        if game.source == "steam" and game.installed:
            self._launch_steam_game(game)
            return
        if game.id is None:
            return
        if self.runtime.running_game is game:
            self._stop_game()
            return
        config = game.merged_config(self.library.global_config())
        try:
            self.runtime.start(game, config)
            self._running_started_monotonic = GLib.get_monotonic_time() / 1e6
        except GameAlreadyRunning as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
        except OSError as error:
            logger.error("Failed to launch %s: %s", game.name, error)
            self.toasts.add_toast(Adw.Toast(title=f"Failed to launch {game.name}: {error.strerror or error}"))
        except Exception:
            logger.exception("Failed to launch %s", game.name)
            self.toasts.add_toast(Adw.Toast(title=f"Failed to launch {game.name}"))

    def _launch_steam_game(self, game: Game) -> None:
        """Launch an installed Steam game via Steam's run-game URI."""
        from gi.repository import Gio

        appid = game.source_id or ""
        if not appid:
            self.toasts.add_toast(Adw.Toast(title=f"No Steam appid for {game.name}"))
            return
        uri = f"steam://rungameid/{appid}"
        try:
            Gio.AppInfo.launch_default_for_uri(uri)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to launch Steam game %s: %s", game.name, error)
            self.toasts.add_toast(Adw.Toast(title=f"Could not launch {game.name} via Steam"))
            return
        self.toasts.add_toast(Adw.Toast(title=f"Launching {game.name} via Steam"))

    def open_store_page(self, game: Game) -> None:
        """Open a store game's page in the system browser."""
        from gi.repository import Gio

        url = f"https://store.steampowered.com/app/{game.source_id}"
        try:
            Gio.AppInfo.launch_default_for_uri(url)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to open store page for %s: %s", game.name, error)
            self.toasts.add_toast(Adw.Toast(title=f"Could not open store page for {game.name}"))

    def _stop_game(self) -> None:
        game = self.runtime.running_game
        self.runtime.stop()
        if game is not None:
            self.toasts.add_toast(Adw.Toast(title=f"Stopping {game.name}"))

    # -- selection -------------------------------------------------------------

    def _on_selection_changed(self, view: LibraryView) -> None:
        self.detail_bar.set_game(view.selected_game())

    def on_tile_context(self, game: Game, _x: float, _y: float) -> None:
        """Right-click on a tile: show a context menu."""
        # Ensure the tile is selected so the detail bar follows.
        tile = None
        child = self.library_view.flow.get_first_child()
        while child is not None:
            if getattr(child, "game", None) is game:
                tile = child
                break
            child = child.get_next_sibling()
        if tile is not None:
            self.library_view.flow.select_child(tile)

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for label, handler in self._context_items(game):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.set_halign(Gtk.Align.FILL)
            button.connect("clicked", lambda _b, h=handler: (h(), popover.popdown()))
            box.append(button)

        popover.set_child(box)
        popover.set_parent(tile or self)
        popover.set_position(Gtk.PositionType.RIGHT)
        popover.popup()

    def _context_items(self, game: Game) -> list[tuple[str, Callable[[], None]]]:
        items: list[tuple[str, Callable[[], None]]] = [
            ("Properties", lambda: self.on_edit_game(game)),
        ]
        if game.source == "steam" and not game.installed:
            items.append(("Open store page", lambda: self.open_store_page(game)))
        items.append(("Remove from library", lambda: self.on_game_removed(game)))
        return items

    # -- runtime callbacks (come from a background thread) ----------------------

    def _marshal(self, fn) -> None:
        GLib.idle_add(fn)

    def _on_game_started(self, game: Game) -> None:
        def apply() -> bool:
            self.toasts.add_toast(Adw.Toast(title=f"Launched {game.name}"))
            return GLib.SOURCE_REMOVE

        self._marshal(apply)

    def _on_game_exited(self, game: Game, hours: float, returncode: int) -> None:
        def apply() -> bool:
            self.library.record_playtime(game, hours)
            self.reload()
            status = "exited" if returncode == 0 else f"exited with code {returncode}"
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} {status}"))
            return GLib.SOURCE_REMOVE

        self._marshal(apply)

    # -- running indicator ------------------------------------------------------

    def setup_running_ticker(self) -> None:
        def tick() -> bool:
            game = self.runtime.running_game
            elapsed = None
            if game is not None and self._running_started_monotonic:
                elapsed = (GLib.get_monotonic_time() / 1e6) - self._running_started_monotonic
            for tile in self._all_tiles():
                tile.set_running(elapsed if tile.game is game else None)
            if self.detail_bar.game() is game:
                self.detail_bar.set_running(elapsed)
            return GLib.SOURCE_CONTINUE

        self._running_started_monotonic = None
        self._ticker = GLib.timeout_add_seconds(1, tick)

    def _all_tiles(self):
        tiles = []
        child = self.library_view.flow.get_first_child()
        while child is not None:
            tiles.append(child)
            child = child.get_next_sibling()
        return tiles