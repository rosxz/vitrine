"""Main window: source sidebar on the left, library grid + detail bar on the right."""

from __future__ import annotations

import glob
import logging
import os
import shlex
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from importlib import resources

from gi.repository import Adw, GLib, Gtk

from ..gpu import apply_gpu_env
from ..library import SHOW_DETAIL_SETTING, SHOW_HIDDEN, Game, Library
from ..paths import secret_dir
from ..running import GameAlreadyRunning, Runtime
from ..sources import registry
from ..sources.epic.auth import EpicTokenStore
from ..sources.gog.auth import GogTokenStore
from ..sources.steam.auth import SteamTokenStore
from ..sources.steam_source import SteamAuthError, SteamSource
from .game_detail_bar import GameDetailBar
from .game_dialogs import AddGameDialog, GameSettingsDialog, PrefixRecreateWindow
from .library_view import LibraryView
from .settings_dialog import SettingsDialog
from .steam_login_dialog import SteamLoginDialog

if TYPE_CHECKING:
    from .log_window import ExecutionLogWindow

logger = logging.getLogger(__name__)

ALL_GAMES = "__all__"
#: Sidebar pseudo-source showing only starred games (below "All games").
FAVORITES = "__favorites__"

#: Setting key (boolean) for the eye button: hide owned-but-not-installed games.
HIDE_NOT_INSTALLED = "hide_not_installed"

#: Bundled monochrome brand marks for store/sidebar entries (SVG files under
#: ``vitrine/ui/style/brand/``). Keyed by source id.
_BRAND_SOURCE_ICONS = {
    "steam": "steam.svg",
    "gog": "gog.svg",
    FAVORITES: "favorite-white.svg",
}


def _brand_icon_path(name: str) -> str:
    """Absolute path to a bundled brand mark SVG under ``vitrine/ui/style/brand``."""
    return str(resources.files("vitrine.ui.style").joinpath("brand", name))
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
    game_res = str(config.get("gamescope_game_res") or "").lower()
    if "x" in game_res:
        width, _, height = game_res.partition("x")
        if width.isdigit() and height.isdigit():
            args += ["-w", width, "-h", height]
    if config.get("gamescope_window_mode") not in (None, "", "windowed"):
        args.append(str(config["gamescope_window_mode"]))
    if config.get("gamescope_output_res"):
        width, _, height = str(config["gamescope_output_res"]).lower().partition("x")
        if width.isdigit() and height.isdigit():
            args += ["-W", width, "-H", height]
    if config.get("gamescope_fps_limiter"):
        args += ["-r", str(config["gamescope_fps_limiter"])]
    if config.get("gamescope_relative_mouse"):
        args.append("--force-grab-cursor")
    # FSR upscaling (opt-in per game). Gamescope applies a sharpness filter while
    # upscaling from a lower internal resolution to the output.
    if config.get("fsr", True):
        sharpness = str(config.get("gamescope_fsr_sharpness") or 4)
        args += ["--fsr-sharpness", sharpness]
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
        # Whether the per-game description/hero bar is shown at all (Settings).
        self.show_detail_bar = bool(library.setting(SHOW_DETAIL_SETTING, True))

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
            on_favorite=self._on_detail_favorite,
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self.library_view)
        content_box.append(self.detail_bar)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(content_box)
        self.toasts.set_hexpand(True)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Vitrine")
        self.title_widget = title
        header.set_title_widget(title)
        # Left-aligned search field (filters within the current view).
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search")
        self.search_entry.set_width_chars(26)
        self.search_entry.set_margin_start(10)
        self.search_entry.connect("search-changed", self.on_search_changed)
        header.pack_start(self.search_entry)

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
        # Whether hidden/blacklisted games are revealed (set via Settings → General).
        self.show_hidden = bool(self.library.setting(SHOW_HIDDEN, False))
        eye_name = "view-reveal-symbolic" if self.hide_not_installed else "view-conceal-symbolic"
        eye_button = Gtk.Button(icon_name=eye_name)
        eye_button.set_tooltip_text("Hide games not installed locally")
        eye_button.connect("clicked", self.on_toggle_hidden)
        header.pack_end(eye_button)
        self.eye_button = eye_button

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
        # A Steam game launched via steam:// is watched via /proc (no Popen); the
        # watcher is non-None while we are tracking one, and the game is surfaced
        # through the running-game/elapsed plumbing so the ticker shows "Playing".
        self.steam_watcher = None
        self._steam_running_game: Game | None = None
        self._epic_running_game: Game | None = None
        self._launch_logs: dict[int, ExecutionLogWindow] = {}

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
        self._add_source_row(FAVORITES, "Favorites", "star-outline-symbolic")
        # Store sources first, then "Local" at the bottom.
        local_source = registry.get("local")
        for source in registry.all():
            if source.id == "local":
                continue
            self._add_source_row(source.id, source.name, source.icon or "application-x-executable-symbolic")
        self._add_source_row(
            "local",
            (local_source.name if local_source else "Local"),
            (local_source.icon if local_source else "go-home-symbolic") or "go-home-symbolic",
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

        brand = _BRAND_SOURCE_ICONS.get(source_id)
        if brand:
            # Bundled monochrome SVG brand mark (Steam / GOG). Sized to match the
            # other (symbolic) nav icons.
            icon = Gtk.Image.new_from_file(_brand_icon_path(brand))
            icon.set_pixel_size(18)
        else:
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
        if source_id == ALL_GAMES:
            self.current_source = None
        elif source_id == FAVORITES:
            self.current_source = FAVORITES
        else:
            self.current_source = source_id
        # Switching source intentionally resets scroll/selection.
        self.reload(preserve_scroll=False)

    def reload(self, *, preserve_scroll: bool = True) -> None:
        self.show_hidden = bool(self.library.setting(SHOW_HIDDEN, False))
        self.show_detail_bar = bool(self.library.setting(SHOW_DETAIL_SETTING, True))
        if not self.show_detail_bar:
            self.detail_bar.set_visible(False)
        if self.current_source == FAVORITES:
            games = self.library.favorite_games()
        else:
            games = self.library.games(source=self.current_source)
        # Local games are by definition installed on this machine.
        for game in games:
            if game.source == "local" and not game.installed:
                game.installed = True
        # Hidden/blacklisted games are suppressed unless "show hidden" is on.
        if not self.show_hidden:
            games = [g for g in games if not g.hidden]
        if self.hide_not_installed:
            games = [g for g in games if g.installed or not g.source]
        # Search filters within the current view.
        query = (self.search_entry.get_text() or "").strip().lower()
        searching = bool(query)
        if query:
            games = [g for g in games if query in (g.name or "").lower()]
        # While searching, don't auto-select the first result (that would pop
        # the detail/hover panel open on every keystroke).
        self.library_view.set_games(
            games, preserve_scroll=preserve_scroll, auto_select=not searching
        )
        self.title_widget.set_subtitle(
            "1 game" if len(games) == 1 else f"{len(games)} games"
        )
        # Store sources get a refresh button instead of "add a game".
        if self.current_source and self.current_source not in ("local", FAVORITES):
            self.add_button.set_visible(False)
            self.refresh_button.set_visible(True)
        else:
            self.add_button.set_visible(True)
            self.refresh_button.set_visible(False)
        self._restore_download_state()

    def on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        """Refilter the current view as the user types."""
        self.reload()

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
        dialog = SettingsDialog(
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
        )
        # Settings can change library-wide flags (e.g. "show hidden games"), so
        # refresh the grid when the settings window goes away.
        dialog.connect("close-request", self._on_settings_closed)
        dialog.present()

    def _on_settings_closed(self, _dialog) -> bool:
        self.reload()
        return False

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
            if gogdl.has_manifest(game_id):
                install_directory = gogdl.manifest_data(game_id).get("installDirectory")
                repair_path = os.path.join(install_path, str(install_directory)) if install_directory else install_path
                os.makedirs(repair_path, exist_ok=True)
                command = gogdl.repair_command(game_id, repair_path, auth_path)
            else:
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

    def _gog_finish_install(self, game: Game, output: Sequence[str] = ()) -> None:
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
            already_downloaded = gogdl.reported_nothing_to_do(output)
            GLib.idle_add(
                self.toasts.add_toast,
                Adw.Toast(
                    title=(
                        f"{game.name}: gogdl reported existing content, but its manifest was not found"
                        if already_downloaded
                        else f"{game.name}: gogdl finished without an install manifest"
                    )
                ),
            )
            return
        game.installed = True

        # Best-effort: read the executable / info from the gogdl manifest.
        info = {}
        try:
            info = gogdl.import_info(game_id, game_root, self._gog_auth_path())
        except Exception:  # noqa: BLE001
            info = {}
        exe = gogdl.executable_from_info(info, game_root)
        if exe is None:
            exe = gogdl.find_executable(game_root)
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

    # -- download state ---------------------------------------------------------

    def _start_download(
        self, game: Game, command: list[str], *, log: bool = True, timeout: float | None = None, cwd: str | None = None
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
            cwd=cwd,
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
        job = self._downloads.get(game.id) if game.id is not None else None
        output = list(getattr(job, "line_buffer", ()))
        is_gog = game.source == "gog"
        from ..sources.gog import gogdl

        already_downloaded = is_gog and gogdl.reported_nothing_to_do(output)
        self._set_downloading_ui(game, False)
        if returncode != 0 and not already_downloaded:
            self.toasts.add_toast(Adw.Toast(title=f"Install failed for {game.name} ({returncode})"))
            return
        # Re-sync installed state so legendary's (now-installed) games mark the
        # library rows as installed and route to Launch instead of Install.
        try:
            if game.source == "epic":
                from ..sources.epic_source import EpicSource

                EpicSource(self.library).sync_installed()
            elif is_gog:
                self._gog_finish_install(game, output)
        except Exception:  # noqa: BLE001
            logger.exception("sync_installed after install failed")
        if not is_gog:
            # GOG's own completion shows its toast/reload.
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"Installed {game.name}"))

    # -- Epic Games Store process buttons ---------------------------------------

    # -- asynchronous artwork -------------------------------------------------

    _ART_WORKERS = 8

    def _start_artwork_fetch(self, games: Sequence[Game]) -> None:
        """Fetch artwork for many games on a worker pool, off the UI thread."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from ..artwork import fetch_game_artwork, load_context

        # Snapshot credentials + priority on the main thread; workers must not
        # touch the library's sqlite connection.
        art_context = load_context(self.library)

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
                changed = fetch_game_artwork(game, force=False, library=self.library, ctx=art_context)
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
        self._set_detail_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Added {game.name}"))

    def _on_detail_play(self, game: Game | None) -> None:
        if game is not None:
            self.on_game_activated(game)

    def _on_detail_settings(self, game: Game | None) -> None:
        if game is not None:
            self.on_edit_game(game)

    def _on_detail_favorite(self, game: Game | None) -> None:
        if game is None:
            return
        starred = not bool(game.favorite)
        self.set_game_favorite(game, starred)
        self.toasts.add_toast(Adw.Toast(title=f"{'Starred' if starred else 'Unstarred'} {game.name}"))

    def set_game_favorite(self, game: Game, favorite: bool) -> None:
        """Persist a favorite toggle without rebuilding the grid (no flash).

        Un-favoriting while the Favorites view is active removes that game's tile
        in place; otherwise nothing in the grid changes, only the hero star.
        """
        self.library.set_favorite(game.id, favorite)
        game.favorite = favorite
        if self.detail_bar.game() is game:
            self.detail_bar.set_favorite(favorite)
        if self.current_source == FAVORITES and not favorite:
            if self.library_view.remove_game(game):
                self._update_visible_count()

    def set_game_hidden(self, game: Game, hidden: bool) -> None:
        """Hide/blacklist (or unhide) a game without rebuilding the grid.

        When "show hidden" is off the tile is removed in place (scroll position
        is untouched); when on, the tile just dims. No reload → no white flash.
        """
        self.library.set_hidden(game.id, hidden)
        game.hidden = hidden
        if self.show_hidden:
            self.library_view.set_hidden_visual(game, hidden)
        else:
            if self.library_view.remove_game(game):
                self._update_visible_count()

    def _update_visible_count(self) -> None:
        count = self.library_view.total_count()
        self.title_widget.set_subtitle("1 game" if count == 1 else f"{count} games")

    def on_edit_game(self, game: Game) -> None:
        GameSettingsDialog(
            self.library,
            game,
            on_save=self.on_game_edited,
            on_remove=self.on_game_removed,
            on_refresh_artwork=self._on_refresh_artwork,
            on_pick_artwork=self._on_artwork_chosen,
            on_open_install=self._open_install_dir,
            on_open_prefix=self._open_prefix_dir,
            on_recreate_prefix=self._recreate_prefix,
            on_wine_config=self.open_wine_config,
            parent=self,
        ).present()

    def _on_artwork_chosen(self, game: Game) -> None:
        """Refresh the UI after the artwork picker chose a tile+hero."""
        self.library.update(game)
        self.reload()
        self._set_detail_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Updated artwork for {game.name}"))

    def _on_refresh_artwork(self, game: Game) -> None:
        try:
            from ..artwork import refresh_game_artwork

            changed = refresh_game_artwork(self.library, game, force=True)
        except Exception as exc:  # noqa: BLE001 - surface as a toast, not a crash.
            self.toasts.add_toast(Adw.Toast(title=f"Refresh failed: {exc}"))
            return
        if not changed:
            self._maybe_show_provider_hint()
        if changed:
            self.library.update(game)
            self.reload()
            self._set_detail_game(game)
            self.toasts.add_toast(Adw.Toast(title=f"Updated artwork for {game.name}"))

    def _maybe_show_provider_hint(self) -> None:
        """One-time hint when no artwork provider key is configured."""
        try:
            from ..artwork import mark_provider_hint_shown, provider_hint_pending

            if provider_hint_pending(self.library):
                mark_provider_hint_shown(self.library)
                self.toasts.add_toast(
                    Adw.Toast(
                        title="Configure IGDB/SteamGridDB keys in Settings → Appearance for richer artwork."
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("provider hint")

    def on_game_edited(self, game: Game) -> None:
        self.reload()
        self._set_detail_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Updated {game.name}"))

    def on_game_removed(self, game: Game) -> None:
        """'Remove' action for a game.

        Local games are fully removed from the library (Vitrine owns them). Steam
        games are handed to Steam itself (``steam://uninstall/<appid>``) -- Steam
        manages its own install files, so no local prompt is shown. Other store
        games (GOG/Epic) revert to *available but not installed*: we uninstall the
        files on disk (and optionally the prefix, after a confirmation) but keep
        the library entry so it stays reinstallable.
        """
        if game.source == "local":
            self.library.remove(game.id) if game.id is not None else None
            self.reload()
            self._set_detail_game(None)
            self.toasts.add_toast(Adw.Toast(title=f"Removed {game.name}"))
            return
        if game.source == "steam":
            self._uninstall_steam_game(game)
            return
        self._prompt_uninstall(game)

    def _uninstall_steam_game(self, game: Game) -> None:
        """Uninstall a Steam game through Steam itself (no local prompt).

        Steam owns the install directory, so Vitrine asks Steam to uninstall the
        title and marks it *not installed* so the UI reflects it immediately. If
        the user cancels the uninstall in Steam, a library refresh re-detects it
        (the Steam source reconciles installed-ness from the app manifests).
        """
        from gi.repository import Gio

        appid = game.source_id or ""
        if not appid:
            self.toasts.add_toast(Adw.Toast(title=f"No Steam appid for {game.name}"))
            return
        uri = f"steam://uninstall/{appid}"
        try:
            Gio.AppInfo.launch_default_for_uri(uri)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to uninstall %s via Steam: %s", game.name, error)
            self.toasts.add_toast(Adw.Toast(title=f"Could not uninstall {game.name} via Steam"))
            return

        # Reflect the pending uninstall locally; the next Steam sync reconciles.
        game.installed = False
        if game.id is not None:
            self.library.update(game)
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"Uninstalling {game.name} via Steam"))

    def _prompt_uninstall(self, game: Game) -> None:
        """Ask whether to delete the game prefix along with its install files."""
        from gi.repository import Adw

        dialog = Adw.AlertDialog(
            heading=f"Uninstall {game.name}?",
            body=(
                "This removes the installed game files. Your library entry is "
                "kept, so the game stays available to reinstall at any time.\n\n"
                "Do you also want to delete this game's Wine/Proton prefix?"
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("files", "Remove files")
        dialog.add_response("files_prefix", "Remove files and prefix")
        dialog.set_response_appearance("files", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("files_prefix", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_uninstall_response, game)
        dialog.present(self)

    def _on_uninstall_response(self, dialog, response: str, game: Game) -> None:
        if response in ("files", "files_prefix"):
            self._uninstall_game(game, remove_prefix=(response == "files_prefix"))

    def _uninstall_game(self, game: Game, remove_prefix: bool) -> None:
        """Uninstall a store game's files (+ prefix) and revert to not-installed."""
        from ..sources.epic import legendary as lg

        if game.source == "epic" and game.source_id and lg.is_installed():
            # Legendary tracks its own installs; let it remove the files (and any
            # leftover metadata) so it no longer reports the app as installed.
            try:
                lg.uninstall(game.source_id)
            except Exception:  # noqa: BLE001
                logger.exception("legendary uninstall failed for %s", game.name)
                self.toasts.add_toast(Adw.Toast(title=f"Could not uninstall {game.name}"))
                return
        else:
            install_dir = self._game_install_dir(game)
            if install_dir:
                try:
                    shutil.rmtree(install_dir, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    logger.exception("removing install dir for %s", game.name)

        if remove_prefix:
            from ..launch import wine_prefix_for

            prefix = str(wine_prefix_for(game))
            if os.path.isdir(prefix):
                try:
                    shutil.rmtree(prefix, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    logger.exception("removing prefix for %s", game.name)

        # Keep the library entry, but revert it to 'available, not installed'.
        game.installed = False
        if game.executable is not None:
            game.executable = None
        if game.id is not None:
            self.library.update(game)
        self.reload()
        self._set_detail_game(None)
        self.toasts.add_toast(Adw.Toast(title=f"Uninstalled {game.name}"))

    def on_game_activated(self, game: Game) -> None:
        # Clicking a game that is currently running toggles it off: the "Playing"
        # hero/tile acts as a Stop button (force-closes the game and its wrapper,
        # e.g. gamescope). Compared by identity of the library entry, not object
        # instance -- tiles are rebuilt after reloads with fresh Game objects.
        running = self.runtime.running_game or self._steam_running_game or self._epic_running_game
        if running is not None and self._is_same_game(running, game):
            self._stop_game()
            return
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
        if self._steam_running_game is not None or self._epic_running_game is not None:
            self.toasts.add_toast(Adw.Toast(title="Close the running Steam game first"))
            return
        config = game.merged_config(self.library.global_config())
        try:
            from ..library import DEBUG_LOG_SETTING
            from ..runners import load_runners_store

            # Debug log window (local/GOG games): stream the game's output into
            # it so issues are visible regardless of source.
            log = None
            if self.library.setting(DEBUG_LOG_SETTING, False):
                from .log_window import ExecutionLogWindow

                log = ExecutionLogWindow(f"Launching {game.name}", parent=self)
                log.present()

            from ..launch import LaunchPlan

            def report_plan(plan: LaunchPlan) -> None:
                if log is None:
                    return
                for line in plan.debug_lines():
                    log.append_line(line)

            plan_reporter: Callable[[LaunchPlan], None] | None = (
                report_plan if log is not None else None
            )
            if log is not None and game.id is not None:
                self._launch_logs[game.id] = log
            self.runtime.start(game, config, load_runners_store(self.library),
                               log=log.append_line if log is not None else None,
                               on_plan=plan_reporter)
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
        """Launch a Steam game via Steam's run-game URI and watch its session.

        Works for installed games and, by prompting Steam to install, for ones
        that are only owned. We don't own the process, so a :class:`SteamSessionWatcher`
        (via /proc) flips the game to "Playing" when Steam starts it and, on exit,
        reads the freshly-written app manifest so playtime updates without a manual
        refresh.
        """
        from gi.repository import Gio

        if self.steam_watcher is not None or self.runtime.running or self._epic_running_game is not None:
            self.toasts.add_toast(Adw.Toast(title="Another game is already running"))
            return
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

        from .. import steamwatch
        from ..sources.steam_source import SteamSource

        source = SteamSource(self.library)
        installdir = source.installed_game_dir(appid)
        self.steam_watcher = steamwatch.SteamSessionWatcher(
            appid,
            installdir=installdir,
            on_start=lambda: self._marshal(lambda: self._on_steam_game_started(game)),
            on_exit=lambda: self._marshal(lambda: self._on_steam_game_exited(game)),
        )
        self.steam_watcher.start()
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
        if self.runtime.running or self._steam_running_game is not None or self._epic_running_game is not None:
            self.toasts.add_toast(Adw.Toast(title="Another game is already running"))
            return

        from ..downloads import run_download
        from ..library import DEBUG_LOG_SETTING, DUMP_LAUNCH_ENV_SETTING
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
        from ..runners import has_x11_driver

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
        exe: str | None = None

        # Ensure the prefix is ready and its architecture matches the runner.
        # A fresh/incompatible prefix is initialised (wineboot) in the background
        # so the game launches on a valid, correctly-arched prefix. For Proton
        # games we now hand the prefix to umu-run, which performs its own full
        # setup, so a manual wineboot is unnecessary (and would fight umu).
        if is_proton:
            from ..launch import _proton_dist_dir
            from ..prefix import stop_wineserver
            from ..wine import umu

            try:
                umu.umu_binary()
            except umu.UmuError as exc:
                self.toasts.add_toast(Adw.Toast(title=str(exc)))
                return
            stop_wineserver(wine_bin, wine_prefix, steam_run=True)
            # For launching, legendary is only needed to resolve the installed
            # executable; the game itself runs through umu-run (steam-run wrapped,
            # clean env) which we've verified works on NixOS. Legendary's own
            # --wrapper/--no-wine path doesn't spawn reliably under steam-run.
            exe = lg.installed_executable(app)
            if not exe:
                self.toasts.add_toast(
                    Adw.Toast(title=f"Could not find the installed executable for {game.name}")
                )
                return
            command = umu.umu_command(exe)
            env = umu.umu_env(
                wine_prefix,
                proton_path=_proton_dist_dir(wine_bin)
                or os.path.dirname(os.path.dirname(os.path.expanduser(wine_bin))),
                game_id=app,
                install_path=os.path.dirname(exe),
            )
            # Do NOT apply driver_env here: its Nix LD_LIBRARY_PATH breaks
            # pressure-vessel. In particular, never surface VK_ICD_FILENAMES --
            # pointing the Vulkan loader at the Nix mesa ICD that pressure-vessel
            # doesn't stage in its sandbox makes DXVK fail to init and the game
            # exits without opening a window. The GL driver paths are safe to
            # carry for legacy wined3d titles.
            from ..gpu import discover as _gpu_discover

            _gpu = _gpu_discover()
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
            # Per-game DXVK toggle: off forces Proton to Wine's built-in D3D
            # translators instead of the Vulkan DXVK renderer.
            if not config.get("dxvk", True):
                off = "d3d10core=n;d3d11=n;dxgi=n"
                env.setdefault("WINEDLLOVERRIDES", "")
                env["WINEDLLOVERRIDES"] = (
                    (env["WINEDLLOVERRIDES"] + ";") if env["WINEDLLOVERRIDES"] else ""
                ) + off
            # Per-game esync/fsync/FSR/EasyAntiCheat switches (same flags the
            # local/Wine path applies).
            from ..launch import apply_performance_env

            apply_performance_env(env, config)
            # Per-game environment variables + locale override (Lutris-style).
            for key, value in (config.get("env") or {}).items():
                if key:
                    env[str(key)] = str(value)
            if config.get("locale"):
                env["LANG"] = str(config["locale"])
                env["LC_ALL"] = str(config["locale"])
            # umu_command already wraps in steam-run so pressure-vessel can build
            # its sandbox.
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

        # Gamescope is opt-in per game. The launch command form that actually
        # presents the window on Wayland is 'steam-run umu-run <exe>' with a clean
        # env and the game's cwd; wrapping umu in gamescope breaks it.
        if config.get("gamescope", False):
            command = _gamescope_wrap(config, command)

        if self.library.setting(DEBUG_LOG_SETTING, False):
            log = ExecutionLogWindow(f"Launching {game.name}", parent=self)
            log.present()
            log.append_line("$ " + shlex.join(command))
        else:
            log = None
        if is_proton:
            # Launch Proton games the same way manual runs do -- a direct
            # subprocess (not a piped download job), the clean umu env, and the
            # game directory as cwd. This is what reliably presents the game
            # window on Wayland. Watch it in the background to clear the launch
            # state on exit.
            import subprocess

            if self.library.setting(DUMP_LAUNCH_ENV_SETTING, False):
                self._dump_launch(command, env)
            # When the debug log is open, capture the game's output and send it
            # to the log window so errors are visible there too. Otherwise leave
            # stdio inherited (so the detached game doesn't block on a full pipe).
            capture = log is not None
            proc = subprocess.Popen(
                command,
                env=env,
                cwd=os.path.dirname(exe) if exe else None,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.STDOUT if capture else None,
                text=True,
                bufsize=1,
            )
            if game.id is not None:
                self._downloads[game.id] = proc
            self._set_epic_running(game)
            threading.Thread(
                target=self._watch_proton_proc,
                args=(proc, game, log, exe, wine_bin, wine_prefix),
                daemon=True,
                name="vitrine-epic-watch",
            ).start()
            self.toasts.add_toast(Adw.Toast(title=f"Launching {game.name}"))
        else:
            self._set_epic_running(game)
            job = run_download(
                command,
                env=env,
                cwd=os.path.dirname(exe) if is_proton and exe else None,
                on_line=log.append_line if log is not None else None,
                # The game is running under the wrapper; on exit, record local
                # playtime and revert the Playing state (no manual refresh).
                done=lambda _rc, g=game: self._marshal(lambda: self._epic_playtime_exit(g)),
            )
            if game.id is not None:
                self._downloads[game.id] = job
            self.toasts.add_toast(Adw.Toast(title=f"Launching {game.name} via legendary"))

    def _clear_launch_state(self, game_id: int | None) -> None:
        if game_id is not None:
            self._downloads.pop(game_id, None)

    def _watch_proton_proc(
        self,
        proc,
        game: Game,
        log=None,
        executable: str | None = None,
        wine_binary: str | None = None,
        wine_prefix: str | None = None,
    ) -> None:
        """Wait for a detached Epic (Proton) process, streaming output.

        ``log`` is the open ExecutionLogWindow (or ``None``). When present, its
        stdout/stderr were captured to pipes, so forward each line here before
        waiting on the process. ``append_line`` is thread-safe and wakes up the
        GTK loop, so this can run on a daemon thread.
        """
        if log is not None and proc.stdout is not None:
            def stream_output() -> None:
                try:
                    for line in proc.stdout:
                        log.append_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - a broken pipe must not crash
                    logger.exception("reading Proton output stream")

            threading.Thread(target=stream_output, daemon=True, name="vitrine-epic-log").start()

        returncode = Runtime._wait_for_exit(proc, executable=executable)
        if log is not None:
            log.append_line(f"[launcher exited with code {returncode}]")
        if wine_binary and wine_prefix:
            from ..prefix import stop_wineserver

            stop_wineserver(wine_binary, wine_prefix, steam_run=True)
        self._marshal(lambda: self._epic_playtime_exit(game))

    def _dump_launch(self, command: list[str], env: dict) -> None:
        """Write the exact Proton launch command + environment to a log file for
        debugging window-presentation issues."""
        try:
            from .. import paths

            dest = paths.cache_dir() / "proton-launch.env"
            dest.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"# {shlex.join(command)}", ""]
            for key in sorted(env):
                lines.append(f"{key}={env[key]}")
            dest.write_text("\n".join(lines) + "\n")
        except Exception:  # noqa: BLE001 - diagnostics must never crash
            logger.exception("could not dump proton launch env")

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

    def _recreate_prefix(self, game: Game) -> None:
        """Open an independent confirmation window for rebuilding the prefix."""
        from ..prefix import recreate_prefix_for_game

        def on_started() -> None:
            self._marshal(
                lambda: self.toasts.add_toast(
                    Adw.Toast(title=f"Re-creating prefix for {game.name}")
                )
            )

        def on_finished(error: Exception | None) -> None:
            if error is not None:
                message = str(error)
                self._marshal(
                    lambda: self.toasts.add_toast(
                        Adw.Toast(title=f"Could not re-create prefix: {message}")
                    )
                )
                return
            self._marshal(
                lambda: self.toasts.add_toast(
                    Adw.Toast(title=f"Re-created prefix for {game.name}")
                )
            )

        PrefixRecreateWindow(
            game,
            on_confirm=lambda: recreate_prefix_for_game(
                game,
                self.library,
                on_started=on_started,
                on_finished=on_finished,
            ),
            parent=self,
        ).present()

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
        if self._epic_running_game is not None:
            self._stop_epic_game()
            return
        if self._steam_running_game is not None:
            self._stop_steam_game()
            return
        game = self.runtime.running_game
        self.runtime.stop()
        if game is not None:
            self.toasts.add_toast(Adw.Toast(title=f"Stopping {game.name}"))

    # -- selection -------------------------------------------------------------

    def _on_selection_changed(self, view: LibraryView) -> None:
        self._set_detail_game(view.selected_game())

    def _set_detail_game(self, game: Game | None) -> None:
        """Show the description/hero bar, unless globally disabled in Settings."""
        if self.show_detail_bar or game is None:
            self.detail_bar.set_game(game)
        else:
            self.detail_bar.set_visible(False)

    @staticmethod
    def _is_same_game(a: Game, b: Game) -> bool:
        """Whether two Game objects denote the same library entry (id, or the
        source/source_id pair, or the name as a last resort)."""
        if a.id is not None and b.id is not None:
            return a.id == b.id
        a_key = (a.source, a.source_id)
        b_key = (b.source, b.source_id)
        if a.source_id and b.source_id and a_key == b_key:
            return True
        return (a.name or "").casefold() == (b.name or "").casefold()

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
        ]
        fav_label = "Remove from favorites" if game.favorite else "Add to favorites"
        items.append((fav_label, lambda: self.set_game_favorite(game, not game.favorite)))
        hide_label = "Unhide game" if game.hidden else "Hide game"
        items.append((hide_label, lambda: self.set_game_hidden(game, not game.hidden)))
        if game.source == "steam" and not game.installed:
            items.append(("Open store page", lambda: self.open_store_page(game)))
            return items
        if game.source != "local" and not game.installed:
            # Owned but not installed: offer install for Epic/GOG, store link else.
            if game.source in ("epic", "gog"):
                items.append(("Install…", lambda: self.install_game(game)))
            items.append(("Open store page", lambda: self.open_store_page(game)))
            return items
        # Locally installed (local games or installed store games).
        label = "Remove from library" if game.source == "local" else "Uninstall…"
        items.append((label, lambda: self.on_game_removed(game)))
        return items

    def _open_directory(self, path: str) -> None:
        """Open a directory in the system file manager via xdg-open."""
        import subprocess

        if not path or not os.path.isdir(path):
            self.toasts.add_toast(Adw.Toast(title="Game directory not found"))
            return
        try:
            subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not open directory %s: %s", path, exc)
            self.toasts.add_toast(Adw.Toast(title=f"Could not open {path}"))

    def _open_prefix_dir(self, game: Game) -> None:
        """Reveal the game's Wine/Proton prefix directory in the file manager."""
        from ..launch import wine_prefix_for

        self._open_directory(str(wine_prefix_for(game)))

    def _open_install_dir(self, game: Game) -> None:
        """Reveal the game's installation directory in the file manager."""
        directory = self._game_install_dir(game)
        if directory:
            self._open_directory(directory)

    def _game_install_dir(self, game: Game) -> str | None:
        """Resolve the directory where the game's files actually live."""
        if game.source == "epic" and game.source_id:
            try:
                from ..sources.epic import legendary as lg

                if lg.is_installed():
                    exe = lg.installed_executable(game.source_id)
                    if exe:
                        return os.path.dirname(exe)
            except Exception:  # noqa: BLE001
                logger.exception("resolving install dir for %s", game.name)
        for candidate in (game.executable, game.working_dir):
            if not candidate:
                continue
            directory = (
                candidate
                if os.path.isdir(candidate)
                else (os.path.dirname(candidate) if os.path.isfile(candidate) else None)
            )
            if directory and os.path.isdir(directory):
                return directory
        return None

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
            log = self._launch_logs.pop(game.id, None) if game.id is not None else None
            if log is not None:
                log.append_line(f"[launcher exited with code {returncode}]")
            self.library.record_playtime(game, hours)
            self.reload()
            status = "exited" if returncode == 0 else f"exited with code {returncode}"
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} {status}"))
            return GLib.SOURCE_REMOVE

        self._marshal(apply)

    # -- Steam session (watched via /proc, no local process) -------------------

    def _on_steam_game_started(self, game: Game) -> None:
        self._steam_running_game = game
        self._running_started_monotonic = GLib.get_monotonic_time() / 1e6
        self.toasts.add_toast(Adw.Toast(title=f"Playing {game.name}"))

    # -- Epic session (launched via legendary, playtime tracked locally) --------

    def _set_epic_running(self, game: Game) -> None:
        """Mark an Epic game as the running session so the ticker shows Playing."""
        self._epic_running_game = game
        self._running_started_monotonic = GLib.get_monotonic_time() / 1e6
        self.toasts.add_toast(Adw.Toast(title=f"Playing {game.name}"))

    def _epic_playtime_exit(self, game: Game) -> None:
        """The Epic-launched process ended: accumulate local playtime (offline,
        like GOG) and revert the Playing state."""
        started = self._running_started_monotonic
        if started:
            hours = (GLib.get_monotonic_time() / 1e6 - started) / 3600.0
            if hours > 0:
                self.library.record_playtime(game, hours)
        if game.id is not None:
            self._downloads.pop(game.id, None)
        if self._epic_running_game is not None:
            self._epic_running_game = None
        self._running_started_monotonic = None
        self._refresh_running_state()
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"{game.name} closed"))

    def _stop_epic_game(self) -> None:
        """Force-stop an Epic game under legendary: kill the tracked job/proc."""
        game = self._epic_running_game
        if game is None:
            return
        job = self._downloads.get(game.id) if game.id is not None else None
        from ..downloads import DownloadJob

        if isinstance(job, DownloadJob):
            job.stop()
        elif job is not None and hasattr(job, "terminate"):
            self._stop_process(job)
        self.toasts.add_toast(Adw.Toast(title=f"Stopping {game.name}"))

    @staticmethod
    def _stop_process(proc) -> None:
        """SIGTERM then SIGKILL a child process tree."""
        from .. import procwatch

        procwatch.terminate_tree(proc.pid)
        import time as _time

        _time.sleep(1.0)
        if proc.poll() is None:
            procwatch.kill_tree(proc.pid)

    def _on_steam_game_exited(self, game: Game) -> None:
        """The Steam-launched process is gone: stop the session and refresh
        playtime from the app manifest Steam wrote on exit (no manual refresh)."""
        if self.steam_watcher is not None:
            self.steam_watcher.stop()
        self.steam_watcher = None
        self._steam_running_game = None
        self._running_started_monotonic = None
        self._refresh_running_state()
        self.toasts.add_toast(Adw.Toast(title=f"{game.name} closed"))
        threading.Thread(target=self._steam_playtime_refresh, args=(game,), daemon=True).start()

    def _steam_playtime_refresh(self, game: Game) -> None:
        from ..sources.steam_source import SteamSource

        appid = game.source_id or ""
        source = SteamSource(self.library)
        hours: float | None = None
        lastplayed: int | None = None
        # Steam writes playtime on exit but may lag a moment; retry briefly.
        # Prefer the freshly-written local manifest; fall back to the Steam Web
        # API (some manifests, e.g. Proton titles, never carry playtime_forever).
        for _ in range(6):
            hours, lastplayed = source.read_manifest_playtime(appid)
            if hours is None:
                try:
                    hours, lastplayed = source.web_playtime(appid)
                except Exception:  # noqa: BLE001 - network hiccups must not kill the refresh
                    logger.exception("Steam web playtime refresh failed for %s", game.name)
                    hours, lastplayed = None, None
            if hours is not None:
                break
            time.sleep(2.0)
        GLib.idle_add(self._apply_steam_playtime, game, hours, lastplayed)

    def _apply_steam_playtime(self, game: Game, hours: float | None, lastplayed: int | None) -> None:
        if hours is None:
            return  # game not installed / no manifest; nothing authoritative to write.
        if game.id is not None:
            fresh = self.library.game(game.id) or game
            fresh.playtime = float(hours)
            if lastplayed is not None:
                fresh.lastplayed = lastplayed
            self.library.update(fresh)
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"Updated playtime for {game.name}"))

    def _stop_steam_game(self) -> None:
        """Force-stop a Steam game: SIGTERM, then SIGKILL if it ignores it."""
        game = self._steam_running_game
        if game is None:
            return
        import time as _time

        from .. import steamwatch

        appid = game.source_id or (self.steam_watcher.appid if self.steam_watcher else "")
        installdir = self.steam_watcher.installdir if self.steam_watcher else None
        sent = steamwatch.terminate_game(appid, installdir) if appid else 0
        if sent and appid:
            # Give the game a moment, then SIGKILL anything still alive.
            _time.sleep(1.0)
            if steamwatch.steam_game_is_running(appid, installdir):
                steamwatch.kill_game(appid, installdir)
                sent = steamwatch.kill_game(appid, installdir)
        self.toasts.add_toast(
            Adw.Toast(title=f"Stopping {game.name}" + ("" if sent else " (no process found)"))
        )

    # -- running indicator ------------------------------------------------------

    def setup_running_ticker(self) -> None:
        def tick() -> bool:
            self._refresh_running_state()
            return GLib.SOURCE_CONTINUE

        self._running_started_monotonic = None
        self._ticker = GLib.timeout_add_seconds(1, tick)

    def _refresh_running_state(self) -> None:
        """Push the current running state to every tile and the hero bar.

        Always refreshes the detail/heard button so a session that ended (e.g. a
        Steam game quit from the game's own menu, leaving no running game) resets
        its label from "Playing · …" back to "Play".
        """
        game = self.runtime.running_game or self._steam_running_game or self._epic_running_game
        elapsed = None
        if game is not None and self._running_started_monotonic:
            elapsed = (GLib.get_monotonic_time() / 1e6) - self._running_started_monotonic
        for tile in self._all_tiles():
            tile.set_running(elapsed if tile.game is game else None)
        if self.detail_bar.game() is game or game is None:
            self.detail_bar.set_running(elapsed)

    def _all_tiles(self):
        tiles = []
        child = self.library_view.flow.get_first_child()
        while child is not None:
            tiles.append(child)
            child = child.get_next_sibling()
        return tiles