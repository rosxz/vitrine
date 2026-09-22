"""Main window: source sidebar on the left, library grid + detail bar on the right."""

from __future__ import annotations

import glob
import logging
import os
import shlex
import threading
from collections.abc import Callable, Sequence

from gi.repository import Adw, GLib, Gtk

from ..gpu import apply_gpu_env
from ..library import Game, Library
from ..paths import secret_dir
from ..running import GameAlreadyRunning, Runtime
from ..sources import registry
from ..sources.epic.auth import EpicTokenStore
from ..sources.gog.auth import GogTokenStore
from ..sources.steam.auth import SteamTokenStore
from ..sources.steam_source import SteamAuthError, SteamSource
from .game_detail_bar import GameDetailBar
from .game_dialogs import AddGameDialog, GameSettingsDialog
from .library_view import LibraryView
from .settings_dialog import SettingsDialog
from .steam_login_dialog import SteamLoginDialog

logger = logging.getLogger(__name__)

ALL_GAMES = "__all__"

#: Setting key (boolean) for the eye button: hide owned-but-not-installed games.
HIDE_NOT_INSTALLED = "hide_not_installed"
#: Idle timeout (seconds without any gogdl output) before we declare a depot
#: download stalled and kill it. gogdl can hang after manifest init when handed
#: a bad token (0 bytes, no output); a short *idle* timeout lets us fall back
#: instead of blocking forever. Healthy downloads stream progress, so this never
#: cuts a working download short.
GOGDL_DOWNLOAD_TIMEOUT = 30.0


def _row_widget_shim(widget: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_margin_top(4)
    box.set_margin_bottom(4)
    box.set_margin_start(16)
    box.set_margin_end(16)
    box.append(widget)
    return box


def _gamescope_wrap(config: dict, command: list[str]) -> list[str]:
    """Prepend a gamescope session running on the HOST display.

    Gamescope provides a nested virtual display + GPU context, the standard way
    to run Windows games under Wayland. It must run on the host compositor (not
    inside a bwrap) so its output window is actually visible. Options come from
    the per-game/global ``config``; only enabled gamescope is passed here.
    """
    args: list[str] = ["gamescope"]
    if config.get("gamescope_output_res"):
        width, height = str(config["gamescope_output_res"]).split("x")
        args += ["-W", width, "-H", height]
    if config.get("gamescope_fps_limiter"):
        args += ["-r", str(config["gamescope_fps_limiter"])]
    return args + ["--", *command]


def _tiles_for(library_view, game) -> list:
    """Find every GameTile widget in the grid representing ``game``."""
    from .library_view import GameTile

    tiles: list = []
    child = library_view.flow.get_first_child()
    while child is not None:
        if isinstance(child, GameTile) and getattr(child, "game", None) is game:
            tiles.append(child)
        child = child.get_next_sibling()
    return tiles


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
        refresh_button.set_tooltip_text("Refresh library")
        refresh_button.connect("clicked", self.on_refresh_source)
        refresh_button.set_visible(False)
        header.pack_end(refresh_button)
        self.refresh_button = refresh_button

        cog = Gtk.Button(icon_name="emblem-system-symbolic")
        cog.set_tooltip_text("Settings")
        cog.connect("clicked", self.on_settings_clicked)
        header.pack_end(cog)
        self.cog_button = cog

        # Toggle to hide games that are not installed locally (owned-but-not
        # downloaded store titles such as Steam). Eye icon reflects the state.
        self.hide_not_installed = bool(self.library.setting(HIDE_NOT_INSTALLED, False))
        eye_name = "view-reveal-symbolic" if self.hide_not_installed else "view-conceal-symbolic"
        eye_button = Gtk.Button(icon_name=eye_name)
        eye_button.set_tooltip_text("Hide games not installed locally")
        eye_button.connect("clicked", self.on_toggle_hidden)
        header.pack_end(eye_button)
        self.eye_button = eye_button

        # Epic-store process controls, top-left (opposite the refresh/scan
        # cluster on the right). Shown only when the Epic source is active.
        self.store_buttons = self._build_store_buttons()
        for button in self.store_buttons:
            header.pack_start(button)
        self._set_store_buttons_visible(False)

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

        # Active downloads keyed by game id (drives tile/detail download state).
        self._downloads: dict[int, object] = {}

        self.reload()

    # -- UI construction -------------------------------------------------------

    def _build_store_buttons(self) -> list[Gtk.Button]:
        """Launch / Focus / Kill buttons for the background Epic Games Store."""
        specs = [
            ("media-playback-start-symbolic", "Launch Epic Games Store", self.on_epic_store_launch),
            ("video-display-symbolic", "Focus Epic Games Store", self.on_epic_store_focus),
            ("process-stop-symbolic", "Kill Epic Games Store", self.on_epic_store_kill),
        ]
        buttons: list[Gtk.Button] = []
        for icon, tooltip, handler in specs:
            button = Gtk.Button(icon_name=icon)
            button.set_tooltip_text(tooltip)
            button.add_css_class("flat")
            button.connect("clicked", handler)
            buttons.append(button)
        return buttons

    def _set_store_buttons_visible(self, visible: bool) -> None:
        for button in self.store_buttons:
            button.set_visible(visible)

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
        # Store sources first, then "Local" at the bottom.
        local_source = registry.get("local")
        for source in registry.all():
            if source.id == "local":
                continue
            self._add_source_row(source.id, source.name, source.icon or "application-x-executable-symbolic")
        self._add_source_row(
            "local",
            (local_source.name if local_source else "Local"),
            (local_source.icon if local_source else "folder-symbolic") or "folder-symbolic",
        )

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.source_list)
        scroller.set_vexpand(True)
        sidebar.append(scroller)

# Default Proton / Wine selector, pinned to the bottom.
        self._runner_ids: list[str] = []
        runner_label = Gtk.Label(label="Default Proton", halign=Gtk.Align.START)
        runner_label.add_css_class("caption")
        runner_label.set_margin_start(12)
        runner_label.set_margin_top(8)
        self.default_runner_dropdown = Gtk.DropDown()
        self.default_runner_dropdown.set_margin_top(2)
        self.default_runner_dropdown.set_margin_start(12)
        self.default_runner_dropdown.set_margin_end(12)
        manage = Gtk.Button(label="Manage Proton…")
        manage.connect("clicked", self.on_manage_proton)
        manage.set_halign(Gtk.Align.FILL)
        manage.set_margin_start(12)
        manage.set_margin_end(12)
        manage.set_margin_top(4)
        manage.set_margin_bottom(8)
        sidebar.append(runner_label)
        sidebar.append(self.default_runner_dropdown)
        sidebar.append(_row_widget_shim(manage))
        self._refresh_runner_dropdown()

        # Artwork-fetch progress, shown only while a background fetch runs.
        self.art_progress = Gtk.ProgressBar()
        self.art_progress.set_show_text(True)
        self.art_progress.set_margin_top(6)
        self.art_progress.set_margin_bottom(8)
        self.art_progress.set_margin_start(10)
        self.art_progress.set_margin_end(10)
        self.art_progress.set_visible(False)
        sidebar.append(self.art_progress)

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
        # Local games are by definition installed on this machine.
        for game in games:
            if game.source == "local" and not game.installed:
                game.installed = True
        if self.hide_not_installed:
            games = [g for g in games if g.installed or not g.source]
        self.library_view.set_games(games)
        self.title_widget.set_subtitle(
            "1 game" if len(games) == 1 else f"{len(games)} games"
        )
        # Store sources get a refresh button instead of "add a game".
        if self.current_source and self.current_source != "local":
            self.add_button.set_visible(False)
            self.refresh_button.set_visible(True)
        else:
            self.add_button.set_visible(True)
            self.refresh_button.set_visible(False)
        self._set_store_buttons_visible(self.current_source == "epic")
        self._restore_download_state()

    def _restore_download_state(self) -> None:
        """Re-apply the "downloading" visuals to tiles after a rebuild."""
        if not getattr(self, "_downloads", None):
            return
        by_id = {g.id: g for g in self.library.games() if g.id in self._downloads}
        for game in by_id.values():
            for tile in _tiles_for(self.library_view, game):
                tile.set_downloading(True)
            if hasattr(self, "detail_bar") and self.detail_bar.game() is game:
                self.detail_bar.set_downloading(True)

    def on_toggle_hidden(self, _button: Gtk.Button) -> None:
        """Toggle hiding owned-but-not-installed games."""
        self.hide_not_installed = not self.hide_not_installed
        self.library.set_setting(HIDE_NOT_INSTALLED, self.hide_not_installed)
        self.eye_button.set_icon_name(
            "view-reveal-symbolic" if self.hide_not_installed else "view-conceal-symbolic"
        )
        self.reload()

    def _refresh_runner_dropdown(self) -> None:
        from ..runners import DEFAULT_PROTON_SETTING, list_runners, load_runners_store

        runners = list_runners(load_runners_store(self.library))
        self._runner_ids = [r.id for r in runners]
        names = [r.name for r in runners]
        self.default_runner_dropdown.set_model(Gtk.StringList.new(names))
        current = str(self.library.setting(DEFAULT_PROTON_SETTING, "wine-64") or "wine-64")
        if current in self._runner_ids:
            self.default_runner_dropdown.set_selected(self._runner_ids.index(current))
        elif self._runner_ids:
            self.default_runner_dropdown.set_selected(0)
        self.default_runner_dropdown.connect("notify::selected", self._on_default_runner_selected)

    def _on_default_runner_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        from ..runners import DEFAULT_PROTON_SETTING

        index = dropdown.get_selected()
        if 0 <= index < len(self._runner_ids):
            self.library.set_setting(DEFAULT_PROTON_SETTING, self._runner_ids[index])

    def on_manage_proton(self, _button: Gtk.Button) -> None:
        from .proton_window import ProtonWindow

        ProtonWindow(self.library, on_changed=self._refresh_runner_dropdown, parent=self).present()

    def on_settings_clicked(self, _button: Gtk.Button) -> None:
        SettingsDialog(
            self.library,
            self.theme_manager,
            on_steam_login=self.on_steam_login,
            on_steam_refresh=self._run_steam_sync,
            on_steam_reset=self.on_steam_reset_session,
            on_gog_login=self.on_gog_login,
            on_gog_refresh=self._run_gog_sync,
            on_gog_reset=self.on_gog_reset_session,
            on_epic_login=self.on_epic_login,
            on_epic_refresh=self._run_epic_sync,
            on_epic_reset=self.on_epic_reset_session,
            parent=self,
        ).present()

    def on_add_game_clicked(self, _button: Gtk.Button) -> None:
        AddGameDialog(self.library, on_add=self.on_game_added, parent=self).present()

    def on_refresh_source(self, _button: Gtk.Button) -> None:
        if self.current_source == "steam":
            self._run_steam_sync()
        elif self.current_source == "gog":
            self._run_gog_sync()
        elif self.current_source == "epic":
            self._run_epic_sync()
        else:
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
            # Artwork is downloaded asynchronously so hundreds of games never
            # block the UI thread; we only syndicate the work here.
            pending = source.games_needing_artwork()
            if pending:
                self._start_artwork_fetch(pending)
        except SteamAuthError as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
        except Exception as error:  # noqa: BLE001
            logger.exception("Steam sync failed")
            self.toasts.add_toast(Adw.Toast(title=f"Steam sync failed: {error}"))

    # -- GOG -------------------------------------------------------------------

    def on_gog_login(self) -> None:
        """Open the GOG sign-in browser, then refresh the library."""
        from ..sources.gog_source import USER_SETTING, GogSource
        from .gog_login_dialog import GogLoginDialog

        source = GogSource(self.library)

        def on_complete(ok: bool, user_id: str | None = None) -> None:
            if not ok:
                self.toasts.add_toast(Adw.Toast(title="GOG sign-in failed"))
                return
            if user_id:
                self.library.set_setting(USER_SETTING, user_id)
            self.toasts.add_toast(Adw.Toast(title="GOG sign-in complete"))
            self._run_gog_sync()

        store = source.login_token_store()
        dialog = GogLoginDialog(store, on_complete=on_complete, parent=self)
        dialog.present()

    def on_gog_reset_session(self) -> None:
        """Clear stored GOG credentials so the user can sign in afresh."""
        from ..sources.gog_source import USER_SETTING

        cleared = 0
        for path in glob.glob(str(secret_dir() / "gog" / "auth_*.json")):
            user_id = path.rsplit("auth_", 1)[1].rsplit(".json", 1)[0]
            GogTokenStore(secret_dir(), user_id).clear()
            cleared += 1
        self.library.clear_source_games("gog")
        self.library.set_setting(USER_SETTING, None)
        if self.current_source == "gog":
            self.current_source = None
        self.reload()
        self.toasts.add_toast(
            Adw.Toast(title="GOG session reset" if cleared else "No GOG credentials to reset")
        )

    def _run_gog_sync(self) -> None:
        from ..sources.gog_source import USER_SETTING, GogAuthError, GogSource

        try:
            source = GogSource(self.library)
            if not source.is_authenticated():
                self.toasts.add_toast(Adw.Toast(title="Sign in to GOG first (cog → GOG)"))
                return
            if source.user_id:
                self.library.set_setting(USER_SETTING, source.user_id)
            count = source.sync()
            self.current_source = "gog"
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"GOG refreshed · {count} games"))
            pending = source.games_needing_artwork()
            if pending:
                self._start_artwork_fetch(pending)
        except GogAuthError as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
        except Exception as error:  # noqa: BLE001
            logger.exception("GOG sync failed")
            self.toasts.add_toast(Adw.Toast(title=f"GOG sync failed: {error}"))

    # -- Epic -------------------------------------------------------------------

    def on_epic_login(self) -> None:
        """Open the Epic sign-in browser, then refresh the library."""
        from ..sources.epic_source import ACCOUNT_SETTING, EpicSource
        from .epic_login_dialog import EpicLoginDialog

        source = EpicSource(self.library)

        def on_complete(ok: bool, account_id: str | None = None, code: str = "") -> None:
            if not ok:
                self.toasts.add_toast(Adw.Toast(title="Epic sign-in failed"))
                return
            # The log-in dialog already imported the (single-use) exchange code
            # into legendary and persisted the token; we only record the
            # account and refresh the library here. Do NOT call legendary auth
            # again with the same code -- it is consumed once.
            if account_id:
                self.library.set_setting(ACCOUNT_SETTING, account_id)
            self.toasts.add_toast(Adw.Toast(title="Epic sign-in complete"))
            self._run_epic_sync()

        store = source.login_token_store()
        dialog = EpicLoginDialog(store, on_complete=on_complete, parent=self)
        dialog.present()

    def on_epic_reset_session(self) -> None:
        """Clear stored Epic credentials so the user can sign in afresh."""
        from ..sources.epic_source import ACCOUNT_SETTING

        cleared = 0
        for path in glob.glob(str(secret_dir() / "epic" / "auth_*.json")):
            account_id = path.rsplit("auth_", 1)[1].rsplit(".json", 1)[0]
            EpicTokenStore(secret_dir(), account_id).clear()
            cleared += 1
        self.library.clear_source_games("epic")
        self.library.set_setting(ACCOUNT_SETTING, None)
        if self.current_source == "epic":
            self.current_source = None
        self.reload()
        self.toasts.add_toast(
            Adw.Toast(title="Epic session reset" if cleared else "No Epic credentials to reset")
        )

    def _run_epic_sync(self) -> None:
        from ..sources.epic_source import EpicAuthError, EpicSource

        try:
            source = EpicSource(self.library)
            if not source.is_authenticated():
                self.toasts.add_toast(Adw.Toast(title="Sign in to Epic first (cog → Epic)"))
                return
            count = source.sync()
            source.sync_installed()
            self.current_source = "epic"
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"Epic refreshed · {count} games"))
            pending = source.games_needing_artwork()
            if pending:
                self._start_artwork_fetch(pending)
        except EpicAuthError as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
        except Exception as error:  # noqa: BLE001
            logger.exception("Epic sync failed")
            self.toasts.add_toast(Adw.Toast(title=f"Epic sync failed: {error}"))

    def _install_epic_game(self, game: Game) -> None:
        """Install an Epic game through legendary, tracked as a download."""
        from ..sources.epic import legendary as lg

        app = game.source_id or ""
        if not app:
            self.toasts.add_toast(Adw.Toast(title=f"No Epic app id for {game.name}"))
            return
        if not lg.is_installed():
            self.toasts.add_toast(
                Adw.Toast(title="Legendary is required to install Epic games. Install 'legendary' first.")
            )
            return
        if game.id is not None and game.id in self._downloads:
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} is already downloading"))
            return

        if not lg.is_authenticated():
            self.toasts.add_toast(
                Adw.Toast(title=f"{game.name}: legendary is not signed in to Epic. Sign in via cog → Epic first.")
            )
            return
        command = [lg.legendary_binary(), *lg.install_command(app)]
        self._start_download(game, command)
        GLib.idle_add(self._set_downloading_ui, game, True)

    def _install_gog_game(self, game: Game) -> None:
        """Install a GOG game via its depot (gogdl), tracked as a download.

        Mirrors Lutris/Heroic: download the game files directly from the GOG
        CDN/depot manifest into a known directory, then mark it installed. This
        is non-interactive (unlike the offline installer) and reliable.
        """
        from ..sources.gog import gogdl
        from ..sources.gog_source import GogSource
        from ..util import slugify

        game_id = game.source_id or ""
        if not game_id:
            self.toasts.add_toast(Adw.Toast(title=f"No GOG id for {game.name}"))
            return
        if not gogdl.is_installed():
            self.toasts.add_toast(
                Adw.Toast(title="gogdl is required to install GOG games. Install 'gogdl' first.")
            )
            return
        source = GogSource(self.library)
        if not source.is_authenticated():
            self.toasts.add_toast(Adw.Toast(title="Sign in to GOG first (cog → GOG)"))
            return
        if game.id is not None and game.id in self._downloads:
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} is already downloading"))
            return

        try:
            # Refresh the GOG token before handing it to gogdl: gogdl hangs on
            # an expired token (secure_link 401 -> infinite retry).
            source.ensure_fresh_token()
        except Exception as exc:  # noqa: BLE001
            self.toasts.add_toast(Adw.Toast(title=f"GOG session expired — sign in again ({exc})"))
            return

        store = source.login_token_store()
        from .. import paths

        try:
            auth_path = str(paths.cache_dir() / "gogdl-auth.json")
            # Token was just refreshed by ensure_fresh_token; tell gogdl it is
            # brand-new so its expiry check uses the current token.
            gogdl.write_auth_config_now(store, auth_path)
            install_path = gogdl.install_dir(game.slug or slugify(game.name))
            command = gogdl.download_command(game_id, install_path, auth_path)
        except Exception as exc:  # noqa: BLE001
            self.toasts.add_toast(Adw.Toast(title=f"Could not start GOG install for {game.name}: {exc}"))
            return
        os.makedirs(install_path, exist_ok=True)
        # Remember where this game's files land so launch/_finish know it.
        game.config["gog_install_dir"] = install_path
        game.config["gog_id"] = game_id
        if game.id is not None:
            self.library.update(game)
        self._start_download(game, command, timeout=GOGDL_DOWNLOAD_TIMEOUT)
        GLib.idle_add(self._set_downloading_ui, game, True)
        self.toasts.add_toast(Adw.Toast(title=f"Downloading {game.name}…"))

    def _gog_finish_install(self, game: Game) -> None:
        """Mark a GOG game installed after a successful depot download.

        If gogdl produced no game files (e.g. it was handed a bad token and
        stalled, or the depot had nothing to write), fall back to the
        interactive offline installer.
        """
        from ..sources.gog import gogdl
        from ..util import slugify

        game_id = game.config.get("gog_id") or game.source_id or ""
        install_dir = game.config.get("gog_install_dir")
        if not install_dir:
            install_dir = gogdl.install_dir(game.slug or slugify(game.name))
        game_root = gogdl.find_game_dir(game_id, install_dir) if game_id else None
        if not game_root:
            GLib.idle_add(self._set_downloading_ui, game, False)
            GLib.idle_add(
                self.toasts.add_toast,
                Adw.Toast(title=f"{game.name}: gogdl stalled; using the offline installer"),
            )
            self._install_gog_offline(game)
            return
        game.installed = True

        # Best-effort: read the executable / info from the gogdl manifest.
        info = {}
        try:
            info = gogdl.import_info(game_id, game_root, self._gog_auth_path())
        except Exception:  # noqa: BLE001
            info = {}
        exe = gogdl.executable_from_info(info, game_root)
        if exe:
            game.executable = exe

        if game.id is not None:
            self.library.update(game)
        GLib.idle_add(self._set_downloading_ui, game, False)
        GLib.idle_add(self.reload)
        GLib.idle_add(
            self.toasts.add_toast,
            Adw.Toast(
                title=f"Installed {game.name}"
                + ("" if exe else " — set the executable in Properties")
            ),
        )

    def _gog_auth_path(self) -> str:
        """The gogdl auth-config path used by GOG installs."""
        from .. import paths

        return str(paths.cache_dir() / "gogdl-auth.json")

    def _install_gog_offline(self, game: Game) -> None:
        """Fallback: install GOG via its interactive offline installer.

        Used when gogdl's depot download stalls (a known upstream bug on some
        machines). Downloads the installer and runs it under the game's Wine
        prefix, then marks the game installed and detects its executable.
        """
        import subprocess

        from ..sources.gog_source import GogSource
        from ..util import slugify

        game_id = game.source_id or ""
        if not game_id:
            return
        source = GogSource(self.library)
        if not source.is_authenticated():
            GLib.idle_add(self.toasts.add_toast, Adw.Toast(title="Sign in to GOG first"))
            return

        store = source.login_token_store()
        from .. import paths
        from ..sources.gog import installer as gog_installer

        installer_dir = paths.data_dir() / "installers"
        installer_dir.mkdir(parents=True, exist_ok=True)
        GLib.idle_add(self._set_downloading_ui, game, True)

        from ..launch import wine_prefix_for
        from ..runners import load_runners_store, resolve_runner

        config = game.merged_config(self.library.global_config())
        wine_binary = resolve_runner(
            config.get("runner"), load_runners_store(self.library), config.get("wine_binary")
        )
        prefix = str(wine_prefix_for(game))

        def _notify(title: str) -> None:
            GLib.idle_add(self.toasts.add_toast, Adw.Toast(title=title))

        def _worker() -> None:
            try:
                url = gog_installer.offline_installer(store, game_id, game.name)
                dest = installer_dir / f"{slugify(game.name)}.exe"
                _notify(f"Downloading {game.name} installer…")
                gog_installer.download_installer(url, str(dest))
            except Exception as exc:  # noqa: BLE001
                _notify(f"GOG installer download failed for {game.name}: {exc}")
                GLib.idle_add(self._set_downloading_ui, game, False)
                return
            try:
                env = dict(os.environ)
                env["WINEPREFIX"] = prefix
                os.makedirs(prefix, exist_ok=True)
                _notify(f"Running {game.name} installer…")
                proc = subprocess.Popen([wine_binary, str(dest)], env=env)

                from ..launch import detect_gog_executable

                proc.wait()
                game.installed = True
                exe = detect_gog_executable(prefix)
                if exe:
                    game.executable = exe
                if game.id is not None:
                    self.library.update(game)
                GLib.idle_add(self._set_downloading_ui, game, False)
                GLib.idle_add(self.reload)
                GLib.idle_add(
                    self.toasts.add_toast,
                    Adw.Toast(
                        title=f"Installed {game.name}"
                        + ("" if exe else " — set the executable in Properties")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                _notify(f"Could not run GOG installer for {game.name}: {exc}")
                GLib.idle_add(self._set_downloading_ui, game, False)

        threading.Thread(target=_worker, daemon=True).start()

    # -- download state ---------------------------------------------------------

    def _start_download(
        self, game: Game, command: list[str], *, log: bool = True, timeout: float | None = None
    ) -> object:
        """Run ``command`` as a tracked download job, optionally streamed to a log."""
        from ..downloads import run_download
        from ..library import DEBUG_LOG_SETTING
        from .log_window import ExecutionLogWindow

        if log and self.library.setting(DEBUG_LOG_SETTING, False):
            window = ExecutionLogWindow(f"Installing {game.name}", parent=self)
            window.present()
        else:
            window = None

        def _on_progress(fraction: float) -> None:
            GLib.idle_add(self._update_download_progress, game, fraction)

        def _on_done(returncode: int) -> None:
            GLib.idle_add(self._finish_download, game, returncode)

        job = run_download(
            command,
            progress=_on_progress,
            done=_on_done,
            on_line=window.append_line if window is not None else None,
            timeout=timeout,
        )
        if game.id is not None:
            self._downloads[game.id] = job
        return job

    def _set_downloading_ui(self, game: Game, active: bool) -> None:
        """Reflect download state on the tile and detail bar."""
        if game.id is not None and not active:
            self._downloads.pop(game.id, None)
        for tile in _tiles_for(self.library_view, game):
            tile.set_downloading(active)
        if self.detail_bar.game() is game:
            self.detail_bar.set_downloading(active)

    def _update_download_progress(self, game: Game, fraction: float) -> None:
        for tile in _tiles_for(self.library_view, game):
            tile.set_download_progress(fraction)

    def _finish_download(self, game: Game, returncode: int) -> None:
        """Install finished (or failed): clear download state and toast."""
        self._set_downloading_ui(game, False)
        if returncode != 0:
            self.toasts.add_toast(Adw.Toast(title=f"Install failed for {game.name} ({returncode})"))
            return
        # Re-sync installed state so legendary's (now-installed) games mark the
        # library rows as installed and route to Launch instead of Install.
        is_gog = game.source == "gog"
        try:
            if game.source == "epic":
                from ..sources.epic_source import EpicSource

                EpicSource(self.library).sync_installed()
            elif is_gog:
                self._gog_finish_install(game)
        except Exception:  # noqa: BLE001
            logger.exception("sync_installed after install failed")
        if not is_gog:
            # GOG's own completion shows its toast/reload.
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"Installed {game.name}"))

    # -- Epic Games Store process buttons ---------------------------------------

    def on_epic_store_launch(self, _button: Gtk.Button | None = None) -> None:
        from .epic_store_control import launch_store

        try:
            launch_store()
        except Exception as error:  # noqa: BLE001
            self.toasts.add_toast(Adw.Toast(title=f"Could not launch Epic store: {error}"))

    def on_epic_store_focus(self, _button: Gtk.Button | None = None) -> None:
        from .epic_store_control import focus_store

        try:
            focus_store()
        except Exception as error:  # noqa: BLE001
            self.toasts.add_toast(Adw.Toast(title=str(error)))

    def on_epic_store_kill(self, _button: Gtk.Button | None = None) -> None:
        from .epic_store_control import kill_store

        try:
            kill_store()
        except Exception as error:  # noqa: BLE001
            self.toasts.add_toast(Adw.Toast(title=str(error)))

    # -- asynchronous artwork -------------------------------------------------

    _ART_WORKERS = 8

    def _start_artwork_fetch(self, games: Sequence[Game]) -> None:
        """Fetch artwork for many games on a worker pool, off the UI thread."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from ..artwork import fetch_game_artwork

        total = len(games)
        self._art_total = total
        self._art_done = 0
        self._art_games = {id(game): game for game in games}
        self._art_lock = threading.Lock()

        self.art_progress.set_visible(True)
        self.art_progress.set_text("")
        self.art_progress.set_fraction(0.0)

        def _work(game: Game) -> tuple[Game, bool]:
            try:
                changed = fetch_game_artwork(game, force=False)
            except Exception:  # noqa: BLE001 - one bad game must not kill others.
                logger.exception("Artwork fetch failed for %s", game.name)
                changed = False
            return game, changed

        def _runner() -> None:
            changed_list: list[Game] = []
            try:
                with ThreadPoolExecutor(max_workers=self._ART_WORKERS) as pool:
                    futures = [pool.submit(_work, game) for game in games]
                    for future in as_completed(futures):
                        game, changed = future.result()
                        with self._art_lock:
                            self._art_done += 1
                        if changed:
                            changed_list.append(game)
                            # Persist freshly-fetched artwork on the main thread.
                            GLib.idle_add(self._on_art_progress, id(game), True)
                        else:
                            GLib.idle_add(self._on_art_progress, id(game), False)
            finally:
                self._art_changed = changed_list
                GLib.idle_add(self._on_art_finished)

        self._art_changed: list[Game] = []
        threading.Thread(target=_runner, daemon=True, name="vitrine-artwork").start()

    def _on_art_progress(self, game_id: int, changed: bool) -> None:
        """Main-thread callback: update the progress bar for one finished game."""
        if self._art_total == 0:
            return
        with self._art_lock:
            done = self._art_done
        fraction = min(done / self._art_total, 1.0)
        self.art_progress.set_fraction(fraction)
        self.art_progress.set_text(f"{done}/{self._art_total}")
        if changed:
            game = self._art_games.get(game_id)
            if game is not None:
                try:
                    self.library.update(game)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to persist artwork for %s", game.name)
        return None

    def _on_art_finished(self) -> None:
        """Main-thread callback: hide the progress bar and refresh the grid."""
        self.art_progress.set_visible(False)
        self.art_progress.set_text("")
        if getattr(self, "_art_changed", None):
            self.reload()

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
            on_refresh_artwork=self._on_refresh_artwork,
            on_wine_config=self.open_wine_config,
            parent=self,
        ).present()

    def _on_refresh_artwork(self, game: Game) -> None:
        try:
            from ..artwork import refresh_game_artwork

            changed = refresh_game_artwork(self.library, game, force=True)
        except Exception as exc:  # noqa: BLE001 - surface as a toast, not a crash.
            self.toasts.add_toast(Adw.Toast(title=f"Refresh failed: {exc}"))
            return
        if changed:
            self.library.update(game)
            self.reload()
            self.detail_bar.set_game(game)
            self.toasts.add_toast(Adw.Toast(title=f"Updated artwork for {game.name}"))
        else:
            self.toasts.add_toast(
                Adw.Toast(title=f"No automatic artwork for {game.name} (set a source and slug, or use Local)")
            )

    def on_game_edited(self, game: Game) -> None:
        self.reload()
        self.detail_bar.set_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Updated {game.name}"))

    def on_game_removed(self, game: Game) -> None:
        if game.source in ("steam", "gog", "epic"):
            self.library.remove_source_game(game.source, game.source_id or "")
        else:
            self.library.remove(game.id) if game.id is not None else None
        self.reload()
        self.detail_bar.set_game(None)
        self.toasts.add_toast(Adw.Toast(title=f"Removed {game.name}"))

    def on_game_activated(self, game: Game) -> None:
        # Every Steam game launches through Steam itself (steam://rungameid),
        # installed or not -- for one that isn't installed locally, Steam will
        # prompt to install it. Never route Steam games through the local
        # Wine/Proton pipeline.
        if game.source == "steam":
            self._launch_steam_game(game)
            return
        # Owned-but-not-installed GOG/Epic titles have no local executable; route
        # to install/store rather than launching `wine` with an empty program.
        if game.source in ("gog", "epic") and not game.installed:
            self.install_game(game)
            return
        # Installed Epic games launch through legendary (the storeless client
        # that manages the game's online session), not the generic Wine pipeline.
        if game.source == "epic":
            self._launch_epic_game(game)
            return
        if game.id is None:
            return
        if self.runtime.running_game is game:
            self._stop_game()
            return
        config = game.merged_config(self.library.global_config())
        try:
            from ..runners import load_runners_store

            self.runtime.start(game, config, load_runners_store(self.library))
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
        """Launch a Steam game via Steam's run-game URI.

        Works for installed games and, by prompting Steam to install, for ones
        that are only owned.
        """
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

    def _launch_epic_game(self, game: Game) -> None:
        """Launch an installed Epic game through legendary, with a live log."""
        from ..sources.epic import legendary as lg

        app = game.source_id or ""
        if not app:
            self.toasts.add_toast(Adw.Toast(title=f"No Epic app id for {game.name}"))
            return
        if not lg.is_installed():
            self.toasts.add_toast(
                Adw.Toast(title="Legendary is required to run Epic games. Install 'legendary' first.")
            )
            return
        if game.id is not None and game.id in self._downloads:
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} is already launching"))
            return

        from ..downloads import run_download
        from ..library import DEBUG_LOG_SETTING
        from ..runners import DEFAULT_PROTON_SETTING, get_runner, load_runners_store, resolve_runner
        from .log_window import ExecutionLogWindow

        # Resolve the game's configured Wine/Proton runner: per-game override
        # wins, otherwise the sidebar's "Default Proton" selection; last resort
        # is the merged global config default.
        config = game.merged_config(self.library.global_config())
        store = load_runners_store(self.library)
        runner_id = (
            game.config.get("runner")
            or self.library.setting(DEFAULT_PROTON_SETTING, None)
            or config.get("runner")
        )
        wine_bin = resolve_runner(runner_id, store, config.get("wine_binary"))
        runner = get_runner(runner_id, store)
        is_proton = runner is not None and runner.kind == "proton"
        from ..launch import wine_prefix_for
        from ..runners import has_wayland_driver, has_x11_driver

        if not has_x11_driver(wine_bin) and os.environ.get("WAYLAND_DISPLAY"):
            self.toasts.add_toast(
                Adw.Toast(
                    title=(
                        f"{game.name}: the selected wine has no X11 driver (Wayland-only). "
                        "Pick a Proton runner (e.g. Proton 11.0) from the per-game settings."
                    )
                )
            )

        wine_prefix = str(wine_prefix_for(game))

        # Ensure the prefix is ready and its architecture matches the runner.
        # A fresh/incompatible prefix is initialised (wineboot) in the background
        # so the game launches on a valid, correctly-arched prefix. For Proton
        # games we now hand the prefix to umu-run, which performs its own full
        # setup, so a manual wineboot is unnecessary (and would fight umu).
        if is_proton:
            from ..launch import _proton_dist_dir
            from ..wine import umu

            try:
                umu_bin = umu.umu_binary()
            except umu.UmuError as exc:
                self.toasts.add_toast(Adw.Toast(title=str(exc)))
                return
            # Legendary runs the game exe through the wrapper; --no-wine stops
            # legendary from invoking wine itself (umu does that for us).
            command = [
                lg.legendary_binary(),
                *lg.launch_command(app, wrapper=umu_bin),
                "--no-wine",
            ]
            env = umu.umu_env(
                wine_prefix,
                proton_path=_proton_dist_dir(wine_bin)
                or os.path.dirname(os.path.dirname(os.path.expanduser(wine_bin))),
                game_id=app,
            )
            # Do NOT apply driver_env here: its Nix LD_LIBRARY_PATH breaks
            # pressure-vessel. Only surface non-loader driver vars.
            from ..gpu import discover as _gpu_discover

            _gpu = _gpu_discover()
            if _gpu.icd_json:
                env.setdefault("VK_ICD_FILENAMES", _gpu.icd_json)
            if _gpu.dri_dir:
                env.setdefault("LIBGL_DRIVERS_PATH", _gpu.dri_dir)
                env.setdefault("MESA_DRIVER_PATH", _gpu.dri_dir)
            from ..launch import install_d3d_extras

            d3d = install_d3d_extras(wine_prefix)
            if d3d:
                env.setdefault("WINEDLLOVERRIDES", "")
                env["WINEDLLOVERRIDES"] = (
                    env["WINEDLLOVERRIDES"] + ";" if env["WINEDLLOVERRIDES"] else ""
                ) + d3d
            # pressure-vessel needs the FHS environment steam-run provides.
            command = ["steam-run", *command]
        else:
            from ..prefix import prepare_prefix

            try:
                prepare_prefix(wine_bin, wine_prefix, steam_run=False)
            except ValueError as exc:
                self.toasts.add_toast(Adw.Toast(title=str(exc)))
                return
            env = dict(os.environ)
            env["WINEARCH"] = "win64"
            env["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d"
            env = apply_gpu_env(env)
            # DirectX 9/10/11 runtime DLLs so old games work under Wine.
            from ..launch import install_d3d_extras

            d3d_overrides = install_d3d_extras(wine_prefix)
            if d3d_overrides:
                env["WINEDLLOVERRIDES"] += ";" + d3d_overrides

            command = [lg.legendary_binary(), *lg.launch_command(app, wine_bin=wine_bin, wine_prefix=wine_prefix)]

        # Opt-in gamescope on the host: gives wine a virtualized display/GPU and
        # is required for many Windows games on Wayland. Off by default (per-game
        # toggle); when off, Proton presents via umu's own display handling.
        if config.get("gamescope", False):
            command = _gamescope_wrap(config, command)
        elif is_proton and has_wayland_driver(wine_bin):
            # Native Wayland path for Wine-GE/GE-Proton (ships winewayland.drv):
            # lets Proton draw straight to the Wayland compositor, no gamescope.
            env["PROTON_ENABLE_WAYLAND"] = "1"

        if self.library.setting(DEBUG_LOG_SETTING, False):
            log = ExecutionLogWindow(f"Launching {game.name}", parent=self)
            log.present()
            log.append_line("$ " + shlex.join(command))
        else:
            log = None
        job = run_download(
            command,
            env=env,
            on_line=log.append_line if log is not None else None,
            # The game is now running detached under the wrapper; clear the
            # download/launch state once the process exits so a retry works.
            done=lambda _rc, gid=game.id: GLib.idle_add(self._clear_launch_state, gid),
        )
        if game.id is not None:
            self._downloads[game.id] = job
        self.toasts.add_toast(Adw.Toast(title=f"Launching {game.name} via legendary"))

    def _clear_launch_state(self, game_id: int | None) -> None:
        if game_id is not None:
            self._downloads.pop(game_id, None)

    def open_wine_config(self, game: Game) -> None:
        """Open winecfg for the game's prefix so deps/drives can be configured."""
        import subprocess

        from ..prefix import open_winecfg_command, prepare_prefix
        from ..runners import DEFAULT_PROTON_SETTING, get_runner, load_runners_store, resolve_runner
        config = game.merged_config(self.library.global_config())
        store = load_runners_store(self.library)
        runner_id = (
            game.config.get("runner")
            or self.library.setting(DEFAULT_PROTON_SETTING, None)
            or config.get("runner")
        )
        wine_bin = resolve_runner(runner_id, store, config.get("wine_binary"))
        runner = get_runner(runner_id, store)
        is_proton = runner is not None and runner.kind == "proton"
        from ..launch import wine_prefix_for

        wine_prefix = str(wine_prefix_for(game))
        try:
            prepare_prefix(wine_bin, wine_prefix, steam_run=is_proton)
        except ValueError as exc:
            self.toasts.add_toast(Adw.Toast(title=str(exc)))
            return
        cmd, env = open_winecfg_command(wine_bin, wine_prefix, steam_run=is_proton)
        env = apply_gpu_env(env)
        self.toasts.add_toast(Adw.Toast(title=f"Opening Wine config for {game.name}"))
        threading.Thread(
            target=lambda: subprocess.Popen(cmd, env=env),
            daemon=True,
        ).start()

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
        """Right-click actions derived from the tile kind, not the active view.

        A Steam game that isn't installed locally only has Properties/Store, and
        cannot be removed (it lives in Steam's cloud library, not Vitrine's).
        """
        items: list[tuple[str, Callable[[], None]]] = [
            ("Properties", lambda: self.on_edit_game(game)),
            ("Wine Configuration…", lambda: self.open_wine_config(game)),
        ]
        if game.source == "steam" and not game.installed:
            items.append(("Open store page", lambda: self.open_store_page(game)))
            return items
        if game.source != "local" and not game.installed:
            # Owned but not installed: offer install for Epic/GOG, store link else.
            if game.source in ("epic", "gog"):
                items.append(("Install…", lambda: self.install_game(game)))
            items.append(("Open store page", lambda: self.open_store_page(game)))
            return items
        # Locally installed (local games or installed store games): removable.
        items.append(("Remove from library", lambda: self.on_game_removed(game)))
        return items

    def install_game(self, game: Game) -> None:
        """Install an owned but not-yet-installed store game."""
        if game.source == "epic":
            self._install_epic_game(game)
        elif game.source == "gog":
            self._install_gog_game(game)
        else:
            self.open_store_page(game)
            self.toasts.add_toast(Adw.Toast(title="No automated install for this source"))

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