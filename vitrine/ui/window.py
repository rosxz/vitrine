"""Main window: source sidebar on the left, library grid + detail bar on the right."""

from __future__ import annotations

import logging
import os
import shlex
import threading
from collections.abc import Callable, Sequence
from importlib import resources
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from vitrine.infra.gpu import apply_gpu_env
from vitrine.infra.running import GameAlreadyRunning, Runtime
from vitrine.services.library import SHOW_DETAIL_SETTING, SHOW_HIDDEN, Game, Library
from vitrine.sources import registry
from vitrine.sources.steam_source import SteamSource
from vitrine.ui.game_detail_bar import GameDetailBar
from vitrine.ui.game_dialogs import AddGameDialog, GameSettingsDialog, PrefixRecreateWindow
from vitrine.ui.library_view import LibraryView
from vitrine.ui.settings_dialog import SettingsDialog

if TYPE_CHECKING:
    from vitrine.ui.log_window import ExecutionLogWindow

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
    inside a bwrap) so its output window is actually visible. Degates to the
    single :func:`launch.gamescope_wrap` implementation so gamescope flag
    handling is consistent for every source.
    """
    from vitrine.services.launch import gamescope_wrap as _shared_gamescope_wrap

    return _shared_gamescope_wrap(config, command)


def _tiles_for(library_view, game) -> list:
    """Find every GameTile widget in the grid representing ``game``.

    Matches by game id, not object identity: a library reload recreates ``Game``
    instances (e.g. when switching provider lists), so callers holding a stale
    ``Game`` must still target the tile showing the fresh one.
    """
    from vitrine.ui.library_view import GameTile

    game_id = getattr(game, "id", None)
    tiles: list = []
    child = library_view.flow.get_first_child()
    while child is not None:
        if isinstance(child, GameTile):
            tile_game = getattr(child, "game", None)
            if tile_game is not None and game_id is not None and getattr(tile_game, "id", None) == game_id:
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
            on_cancel=self._on_detail_cancel,
            on_store=self._on_detail_store,
            on_achievements=self._on_detail_achievements,
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

        # Unified session/bookkeeping coordinator (GUI-free). Installs register
        # here; the running-game slots are folded onto it in the GameEntry step.
        from vitrine.domain.session import SessionManager

        self.sessions: SessionManager = SessionManager()

        # Active downloads keyed by game id (drives tile/detail download state).
        self._downloads: dict[int, object] = {}
        # Last reported download progress (0..1) per game id, so a library
        # rebuild (e.g. switching provider lists) can restore the bar's value.
        self._download_progress: dict[int, float] = {}
        # A Steam game launched via steam:// is watched via /proc (no Popen); the
        # watcher is non-None while we are tracking one, and the game is surfaced
        # through the running-game/elapsed plumbing so the ticker shows "Playing".
        self.steam_watcher = None
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
                if game.id is not None:
                    tile.set_download_progress(self._download_progress.get(game.id, 0.0))
            if (
                hasattr(self, "detail_bar")
                and self.detail_bar.game() is not None
                and self.detail_bar.game().id == game.id
            ):
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
        from vitrine.services.runners import DEFAULT_PROTON_SETTING, list_runners, load_runners_store

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
        from vitrine.services.runners import DEFAULT_PROTON_SETTING

        index = dropdown.get_selected()
        if 0 <= index < len(self._runner_ids):
            self.library.set_setting(DEFAULT_PROTON_SETTING, self._runner_ids[index])

    def on_manage_proton(self, _button: Gtk.Button) -> None:
        from vitrine.ui.proton_window import ProtonWindow

        ProtonWindow(self.library, on_changed=self._refresh_runner_dropdown, parent=self).present()

    def on_settings_clicked(self, _button: Gtk.Button) -> None:
        dialog = SettingsDialog(
            self.library,
            self.theme_manager,
            on_steam_login=lambda: self.on_source_login("steam"),
            on_steam_refresh=lambda: self._run_sync("steam"),
            on_steam_reset=lambda: self.on_source_reset("steam"),
            on_gog_login=lambda: self.on_source_login("gog"),
            on_gog_refresh=lambda: self._run_sync("gog"),
            on_gog_reset=lambda: self.on_source_reset("gog"),
            on_epic_login=lambda: self.on_source_login("epic"),
            on_epic_refresh=lambda: self._run_sync("epic"),
            on_epic_reset=lambda: self.on_source_reset("epic"),
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

    # -- source login / refresh / reset (generic via SyncService) ---------------

    def on_refresh_source(self, _button: Gtk.Button) -> None:
        if not self.current_source or self.current_source in ("local", ALL_GAMES, FAVORITES):
            return
        self._run_sync(self.current_source)

    def on_source_login(self, source_id: str) -> None:
        """Open the store's login browser for ``source_id``, then refresh."""
        from vitrine.sources import registry
        from vitrine.ui.login_registry import make_login_dialog

        source = registry.get(source_id)(self.library)

        def on_complete(ok: bool, *extras) -> None:
            if not ok:
                self.toasts.add_toast(Adw.Toast(title=f"{source.name} sign-in failed"))
                return
            self.toasts.add_toast(Adw.Toast(title=f"{source.name} sign-in complete"))
            # A login reveals the real account; persist it and refresh.
            source.complete_login(*extras)
            self._run_sync(source_id)

        store = source.auth_store()
        dialog = make_login_dialog(source_id, store, on_complete, parent=self)
        dialog.present()

    def on_source_reset(self, source_id: str) -> None:
        """Clear a source's credentials and stale library rows."""
        from vitrine.sources import registry

        source = registry.get(source_id)(self.library)
        source.reset()
        if self.current_source == source_id:
            self.current_source = None
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"{source.name} session reset"))

    def _run_sync(self, source_id: str) -> None:
        """Refresh ``source_id`` through SyncService (auth-check + artwork)."""
        from vitrine.services.sync import AuthRequired, SyncService

        def on_toast(title: str) -> None:
            self.toasts.add_toast(Adw.Toast(title=title))

        service = SyncService(self.library, on_toast=on_toast)
        try:
            result = service.sync(source_id)
        except AuthRequired as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("%s sync failed", source_id)
            self.toasts.add_toast(Adw.Toast(title=f"{source_id} sync failed: {error}"))
            return
        self.current_source = source_id
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"{source_id} refreshed · {result.count} games"))
        if result.pending_artwork:
            pending = self._source_games_needing_artwork(source_id)
            if pending:
                self._start_artwork_fetch(pending)

    def _source_games_needing_artwork(self, source_id: str) -> list:
        from vitrine.sources import registry

        return registry.get(source_id)(self.library).games_needing_artwork()

    def finish_gog_install(self, game: Game, output: Sequence[str] = ()) -> None:
        """Mark a GOG game installed after a successful depot download (controller
        primitive, invoked by the GOG entry's install-finished strategy).

        If gogdl produced no game files (e.g. it was handed a bad token and
        stalled, or the depot had nothing to write), fall back to the
        interactive offline installer.
        """
        from vitrine.infra.util import slugify
        from vitrine.sources.gog import gogdl

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
        from vitrine.infra import paths

        return str(paths.cache_dir() / "gogdl-auth.json")

    # -- download state ---------------------------------------------------------

    def _start_download(
        self, game: Game, command: list[str], *, log: bool = True, timeout: float | None = None, cwd: str | None = None
    ) -> object:
        """Run ``command`` as a tracked download job, optionally streamed to a log."""
        from vitrine.services.downloads import run_download
        from vitrine.services.library import DEBUG_LOG_SETTING
        from vitrine.ui.log_window import ExecutionLogWindow

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
            self.sessions.add_install(game, job)
        return job

    def _set_downloading_ui(self, game: Game, active: bool) -> None:
        """Reflect download state on the tile and detail bar.

        Matches by game id (not object identity): a library reload recreates
        ``Game`` instances, so the (stale) object captured by the download
        callback must still clear the tile/detail bar holding the fresh one.
        """
        if game.id is not None and not active:
            self._downloads.pop(game.id, None)
            self._download_progress.pop(game.id, None)
            self.sessions.remove_install(game)
        for tile in _tiles_for(self.library_view, game):
            tile.set_downloading(active)
            if active and game.id is not None:
                tile.set_download_progress(self._download_progress.get(game.id, 0.0))
        detail = self.detail_bar.game()
        if detail is not None and game.id is not None and detail.id == game.id:
            self.detail_bar.set_downloading(active)

    # -- controller primitives (used by per-source GameEntry strategies) -------

    def toast(self, title: str) -> None:
        """Show a toast on the main thread."""
        self.toasts.add_toast(Adw.Toast(title=title))

    def log_window(self, title: str):
        """Open a debug log window honoring the debug-log setting, else None."""
        from vitrine.services.library import DEBUG_LOG_SETTING

        if not self.library.setting(DEBUG_LOG_SETTING, False):
            return None
        from vitrine.ui.log_window import ExecutionLogWindow

        window = ExecutionLogWindow(title, parent=self)
        window.present()
        return window

    def run_async(self, fn) -> None:
        """Run ``fn`` on a daemon thread (off the UI thread)."""
        threading.Thread(target=fn, daemon=True).start()

    def apply_game_update(self, game: Game, toast_title: str) -> None:
        """Reload the library and toast, marshalled to the main thread."""
        self._marshal(lambda: (self.reload(), self.toasts.add_toast(Adw.Toast(title=toast_title))))

    def installing(self, game: Game) -> bool:
        """Whether a tracked install/launch download exists for ``game``.

        Tolerant of the short window during window construction when the session
        manager isn't wired up yet (a selection change can fire before the
        ``sessions``/``_downloads`` attributes are created).
        """
        if game.id is not None and getattr(self, "_downloads", None) and game.id in self._downloads:
            return True
        sessions = getattr(self, "sessions", None)
        return sessions is not None and sessions.installing(game)

    def cancel_install(self, game: Game) -> None:
        """Stop an in-progress download and remove any provider state it left.

        Kills the tracked download job, dispatches the source's
        :meth:`on_cancel_install` (so GOG/Epic can clear their partial caches and
        manifests), then resets the downloading UI and library state. Safe to
        call for a game that isn't downloading (no-op).
        """
        if not self.installing(game):
            return
        self.sessions.stop_install(game)
        from vitrine.domain.entry import entry_for

        try:
            entry_for(game, self).on_cancel_install()
        except Exception:  # noqa: BLE001 - never let cleanup block cancel
            logger.exception("cancel cleanup failed for %s", game.name)
        self._set_downloading_ui(game, False)
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"Cancelled download for {game.name}"))

    def gogdl_timeout(self) -> float:
        return GOGDL_DOWNLOAD_TIMEOUT

    def start_install_command(self, game: Game, command: list[str], *, timeout: float | None = None) -> None:
        """Start a tracked install download for ``game`` (controller primitive)."""
        self._start_download(game, command, timeout=timeout)
        GLib.idle_add(self._set_downloading_ui, game, True)

    def run_owned_launch(
        self,
        game: Game,
        command: list[str],
        env: dict,
        cwd: str | None = None,
        *,
        proton: bool,
        exe: str | None = None,
        wine_bin: str | None = None,
        wine_prefix: str | None = None,
    ) -> None:
        """Supervise a game executable launch (generic controller primitive).

        Shared execution for every store that runs the game itself (currently
        Epic via legendary/umu). ``proton`` selects the launch mode — a direct
        Popen (umu) versus a tracked download job (legendary with system wine) —
        and routes running-state, playtime-on-exit and the log window uniformly.
        The per-source command/env building lives in the GameEntry strategy.
        """
        from vitrine.services.downloads import run_download
        from vitrine.services.library import DUMP_LAUNCH_ENV_SETTING

        if self.library.setting(DUMP_LAUNCH_ENV_SETTING, False):
            self._dump_launch(command, env)
        log = self.log_window(f"Launching {game.name}")
        if log is not None:
            log.append_line("$ " + shlex.join(command))

        if proton:
            import subprocess

            capture = log is not None
            proc = subprocess.Popen(
                command,
                env=env,
                cwd=cwd or (os.path.dirname(exe) if exe else None),
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
                cwd=cwd,
                on_line=log.append_line if log is not None else None,
                done=lambda _rc, g=game: self._marshal(lambda: self._epic_playtime_exit(g)),
            )
            if game.id is not None:
                self._downloads[game.id] = job
            self.toasts.add_toast(Adw.Toast(title=f"Launching {game.name} via legendary"))

    def _update_download_progress(self, game: Game, fraction: float) -> None:
        if game.id is not None:
            self._download_progress[game.id] = max(0.0, min(fraction, 1.0))
        for tile in _tiles_for(self.library_view, game):
            tile.set_download_progress(fraction)

    def _finish_download(self, game: Game, returncode: int) -> None:
        """Install finished (or failed): clear download state and toast."""
        # A cancel already removed this job from the active set; the killed
        # process's exit must not be treated as a (re-)install completion.
        if game.id is not None and game.id not in self._downloads:
            return
        job = self._downloads.get(game.id) if game.id is not None else None
        output = list(getattr(job, "line_buffer", ()))
        from vitrine.sources.gog import gogdl

        already_downloaded = game.source == "gog" and gogdl.reported_nothing_to_do(output)
        self._set_downloading_ui(game, False)
        if returncode != 0 and not already_downloaded:
            self.toasts.add_toast(Adw.Toast(title=f"Install failed for {game.name} ({returncode})"))
            return
        # Delegate post-install work (re-sync installed / resolve executable) to
        # the source's GameEntry strategy, which knows how to finish its install.
        from vitrine.domain.entry import entry_for

        try:
            entry_for(game, self).on_install_finished(returncode, output)
        except Exception:  # noqa: BLE001
            logger.exception("install finish failed for %s", game.name)
        if game.source != "gog":
            # GOG's own completion shows its toast/reload.
            self.reload()
            self.toasts.add_toast(Adw.Toast(title=f"Installed {game.name}"))

    # -- Epic Games Store process buttons ---------------------------------------

    # -- asynchronous artwork -------------------------------------------------

    _ART_WORKERS = 8

    def _start_artwork_fetch(self, games: Sequence[Game]) -> None:
        """Fetch artwork for many games on a worker pool, off the UI thread."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from vitrine.services.artwork import fetch_game_artwork, load_context

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

    def _start_achievements_refresh(self, game: Game) -> None:
        """Fetch + persist achievements for one game on a worker thread."""
        from vitrine.services.achievements import load_context, refresh_game_achievements

        ctx = load_context(self.library)

        def _worker() -> None:
            changed = False
            try:
                changed = refresh_game_achievements(self.library, game, ctx)
            except Exception:  # noqa: BLE001
                logger.exception("achievements refresh failed for %s", game.name)
            if not changed:
                return

            def _apply() -> None:
                # Re-read the freshly-persisted game so its achievement_* summary
                # (written onto a new DB object by replace_achievements) is what
                # the detail bar reads, then repaint the grid + hero bar.
                current = self.library.game(game.id) if game.id is not None else None
                if current is not None:
                    game.achievement_count = current.achievement_count
                    game.achievement_unlocked = current.achievement_unlocked
                self.reload()
                self._set_detail_game(game)

            GLib.idle_add(_apply)

        threading.Thread(target=_worker, daemon=True, name="vitrine-achievements").start()

    def on_game_added(self, game: Game) -> None:
        self.library.add(game)
        self.reload()
        self._set_detail_game(game)
        self.toasts.add_toast(Adw.Toast(title=f"Added {game.name}"))

    def _on_detail_play(self, game: Game | None) -> None:
        if game is not None:
            self.on_game_activated(game)

    def _on_detail_cancel(self, game: Game | None) -> None:
        if game is not None:
            self.cancel_install(game)

    def _on_detail_store(self, game: Game | None) -> None:
        if game is not None:
            self.open_store_page(game)

    def _on_detail_achievements(self, game: Game | None) -> None:
        if game is not None:
            self.open_achievements(game)

    def open_achievements(self, game: Game) -> None:
        """Open the achievements viewer for ``game``.

        The viewer fetches fresh data on open when nothing is cached yet, and its
        Refresh button re-fetches on demand; ``on_refreshed`` keeps the hero-bar
        summary in sync afterwards.
        """
        from vitrine.ui.achievements_window import AchievementsWindow

        def _on_refreshed() -> None:
            current = self.library.game(game.id) if game.id is not None else None
            if current is not None:
                game.achievement_count = current.achievement_count
                game.achievement_unlocked = current.achievement_unlocked
            self._set_detail_game(game)

        AchievementsWindow(
            self.library,
            game,
            on_refreshed=_on_refreshed,
            parent=self,
        ).present()

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
            from vitrine.services.artwork import refresh_game_artwork

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
            from vitrine.services.artwork import mark_provider_hint_shown, provider_hint_pending

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
        """'Remove' action for a game, delegated to its source entry.

        Local games are fully removed from the library (Vitrine owns them). Steam
        games are handed to Steam itself (``steam://uninstall/<appid>``). Other
        store games (GOG/Epic) revert to *available but not installed*: we ask
        whether to delete the prefix, uninstall the files, and keep the entry.
        """
        from vitrine.domain.entry import entry_for

        entry_for(game, self).on_uninstall()

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
        """Uninstall a store game's files (+ prefix) and revert to not-installed.

        Delegates to the source's :class:`GameEntry` (Steam/Epic/GOG each know
        how to remove their files), then reflects the library + UI state here.
        """
        if game.id is not None:
            from vitrine.domain.entry import entry_for

            entry_for(game, self).uninstall(remove_prefix=remove_prefix)
        self.reload()
        self._set_detail_game(None)
        self.toasts.add_toast(Adw.Toast(title=f"Uninstalled {game.name}"))

    def on_game_activated(self, game: Game) -> None:
        # Every source shares the same Play flow: stop-if-running, else install,
        # else launch. The per-source behaviour lives in GameEntry subclasses.
        from vitrine.domain.entry import entry_for

        entry_for(game, self).on_launch()

    def is_game_running(self, game: Game) -> bool:
        """Whether ``game`` is the entry currently running (identity-aware)."""
        running = self.sessions.running_game
        return running is not None and self._is_same_game(running, game)

    def stop_game(self) -> None:
        """Stop the currently-running game (any source)."""
        self._stop_game()

    def launch_local(self, game: Game) -> None:
        """Generic Wine/Proton/native launch for local and installed GOG games."""
        if game.id is None:
            return
        if self.sessions.running_game is not None:
            self.toasts.add_toast(Adw.Toast(title="Close the running game first"))
            return
        config = game.merged_config(self.library.global_config())
        self._inject_comet_launch(game, config)
        try:
            from vitrine.services.library import DEBUG_LOG_SETTING
            from vitrine.services.runners import load_runners_store

            # Debug log window (local/GOG games): stream the game's output into
            # it so issues are visible regardless of source.
            log = None
            if self.library.setting(DEBUG_LOG_SETTING, False):
                from vitrine.ui.log_window import ExecutionLogWindow

                log = ExecutionLogWindow(f"Launching {game.name}", parent=self)
                log.present()

            from vitrine.services.launch import LaunchPlan

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
            self.sessions.begin(game, self.runtime)
        except GameAlreadyRunning as error:
            self.toasts.add_toast(Adw.Toast(title=str(error)))
            self.toasts.add_toast(Adw.Toast(title=str(error)))

    def _inject_comet_launch(self, game: Game, config: dict) -> None:
        """Wrap a GOG game launch with comet (GOG Galaxy communication service).

        Only for GOG games, when comet is installed and enabled. Writes a 0600
        wrapper script carrying the GOG tokens, registers the dummy service
        (best-effort) and sets ``config["gog_comet_wrapper"]`` so the launch
        pipeline (build_command) prepends it around the actual game command.
        """
        if game.source != "gog" or getattr(game, "runner", "wine") == "linux":
            return
        from vitrine.infra import paths
        from vitrine.sources.gog import comet as comet_mod

        if not self.library.setting(comet_mod.COMET_ENABLED_SETTING, True):
            return
        if not comet_mod.is_installed():
            return
        try:
            tokens = comet_mod.read_tokens(self.library)
        except Exception:  # noqa: BLE001
            logger.exception("could not read GOG tokens for comet")
            return
        if not tokens.get("access_token"):
            return
        script_dir = paths.cache_dir() / "gog-comet"
        script_path = str(script_dir / f"{game.id or 'game'}.sh")
        comet_mod.write_wrapper(script_path, **tokens)
        config["gog_comet_wrapper"] = script_path
        config["_comet"] = True

    def launch_steam(self, game: Game) -> None:
        self._launch_steam_game(game)

    def remove_local(self, game: Game) -> None:
        """Remove a locally-added game from the library."""
        self.library.remove(game.id) if game.id is not None else None
        self.reload()
        self._set_detail_game(None)
        self.toasts.add_toast(Adw.Toast(title=f"Removed {game.name}"))

    def prompt_uninstall(self, game: Game) -> None:
        self._prompt_uninstall(game)

    def uninstall_steam(self, game: Game) -> None:
        self._uninstall_steam_game(game)

    def _launch_steam_game(self, game: Game) -> None:
        """Launch a Steam game via Steam's run-game URI and watch its session.

        Works for installed games and, by prompting Steam to install, for ones
        that are only owned. We don't own the process, so a :class:`SteamSessionWatcher`
        (via /proc) flips the game to "Playing" when Steam starts it and, on exit,
        reads the freshly-written app manifest so playtime updates without a manual
        refresh.
        """
        from gi.repository import Gio

        if self.sessions.running_game is not None:
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

        from vitrine.infra import steamwatch

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
            from vitrine.infra.prefix import stop_wineserver

            stop_wineserver(wine_binary, wine_prefix, steam_run=True)
        self._marshal(lambda: self._epic_playtime_exit(game))

    def _dump_launch(self, command: list[str], env: dict) -> None:
        """Write the exact Proton launch command + environment to a log file for
        debugging window-presentation issues."""
        try:
            from vitrine.infra import paths

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

        from vitrine.infra.prefix import open_winecfg_command, prepare_prefix
        from vitrine.services.runners import load_runners_store, resolve_game_runner
        config = game.merged_config(self.library.global_config())
        store = load_runners_store(self.library)
        runner, wine_bin = resolve_game_runner(game, config, store, library=self.library)
        is_proton = bool(runner and runner.is_proton)
        from vitrine.services.launch import wine_prefix_for

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
        from vitrine.infra.prefix import recreate_prefix_for_game

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

        from vitrine.domain.entry import entry_for

        url = entry_for(game, self).store_url()
        if not url:
            self.toasts.add_toast(Adw.Toast(title=f"No store page for {game.name}"))
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to open store page for %s: %s", game.name, error)
            self.toasts.add_toast(Adw.Toast(title=f"Could not open store page for {game.name}"))

    def _stop_game(self) -> None:
        running = self.sessions.running_game
        if running is None:
            return
        from vitrine.domain.entry import entry_for

        entry_for(running, self).on_stop()
        self.toasts.add_toast(Adw.Toast(title=f"Stopping {running.name}"))

    # -- selection -------------------------------------------------------------

    def _on_selection_changed(self, view: LibraryView) -> None:
        self._set_detail_game(view.selected_game())

    def _set_detail_game(self, game: Game | None) -> None:
        """Show the description/hero bar, unless globally disabled in Settings.

        Reset the bar's download visuals first, then re-assert them if the
        newly-selected game is actually still installing. This clears any stale
        "Downloading…"/disabled-play state left by an interrupted or finished
        download whose library object was recreated after a reload.
        """
        if (self.show_detail_bar or game is None) and self.detail_bar.game() is not None:
            current = self.detail_bar.game()
            if game is None or current.id != game.id:
                # Switching to a different game: drop any leftover download state.
                self.detail_bar.set_downloading(False)
        if self.show_detail_bar or game is None:
            self.detail_bar.set_game(game)
        else:
            self.detail_bar.set_visible(False)
        if game is not None:
            from vitrine.domain.entry import entry_for

            try:
                has_store = entry_for(game, self).store_url() is not None
            except Exception:  # noqa: BLE001
                has_store = False
            self.detail_bar.set_store_visible(has_store)
            from vitrine.services.achievements import provider_for

            available = provider_for(game) is not None
            self.detail_bar.set_achievements(
                getattr(game, "achievement_count", None) or None,
                getattr(game, "achievement_unlocked", None) or None,
                available=available,
            )
            if self.installing(game):
                self.detail_bar.set_downloading(True)

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

        The launch/install/remove strategy now lives on the source's
        :class:`GameEntry`, so this menu is derived from the entry's capabilities
        instead of hardcoded ``game.source`` branches.
        """
        from vitrine.domain.entry import entry_for

        entry = entry_for(game, self)
        items: list[tuple[str, Callable[[], None]]] = [
            ("Properties", lambda: self.on_edit_game(game)),
        ]
        fav_label = "Remove from favorites" if game.favorite else "Add to favorites"
        items.append((fav_label, lambda: self.set_game_favorite(game, not game.favorite)))
        hide_label = "Unhide game" if game.hidden else "Hide game"
        items.append((hide_label, lambda: self.set_game_hidden(game, not game.hidden)))
        if not game.installed and entry.can_install():
            items.append(("Install…", lambda: self.install_game(game)))
        if self.installing(game):
            items.insert(1, ("Stop download", lambda: self.cancel_install(game)))
        if entry.store_url() is not None:
            items.append(("Open store page", lambda: self.open_store_page(game)))
        if game.source == "local" or game.installed:
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
        from vitrine.services.launch import wine_prefix_for

        self._open_directory(str(wine_prefix_for(game)))

    def _open_install_dir(self, game: Game) -> None:
        """Reveal the game's installation directory in the file manager."""
        directory = self._game_install_dir(game)
        if directory:
            self._open_directory(directory)

    def _game_install_dir(self, game: Game) -> str | None:
        """Resolve the directory where the game's files actually live."""
        from vitrine.domain.entry import entry_for

        return entry_for(game, self).install_dir()

    def install_game(self, game: Game) -> None:
        """Install an owned but not-yet-installed store game (via its entry)."""
        from vitrine.domain.entry import entry_for

        entry = entry_for(game, self)
        entry.on_install() if hasattr(entry, "on_install") else self.open_store_page(game)

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
            self.sessions.end()
            log = self._launch_logs.pop(game.id, None) if game.id is not None else None
            if log is not None:
                log.append_line(f"[launcher exited with code {returncode}]")
            from vitrine.domain.entry import entry_for

            entry_for(game, self).record_exit(hours, returncode, self.library)
            self._maybe_refresh_achievements(game)
            self.reload()
            status = "exited" if returncode == 0 else f"exited with code {returncode}"
            self.toasts.add_toast(Adw.Toast(title=f"{game.name} {status}"))
            return GLib.SOURCE_REMOVE

        self._marshal(apply)

    def _maybe_refresh_achievements(self, game: Game) -> None:
        """Re-fetch achievements after a game exits, per the auto-refresh setting."""
        from vitrine.services.achievements import AUTO_REFRESH_SETTING, provider_for

        if not bool(self.library.setting(AUTO_REFRESH_SETTING, True)):
            return
        if provider_for(game) is None:
            return
        self._start_achievements_refresh(game)

    # -- Steam session (watched via /proc, no local process) -------------------

    def _on_steam_game_started(self, game: Game) -> None:
        self.sessions.begin(game)
        self.toasts.add_toast(Adw.Toast(title=f"Playing {game.name}"))

    # -- Epic session (launched via legendary, playtime tracked locally) --------

    def _set_epic_running(self, game: Game) -> None:
        """Mark an Epic game as the running session so the ticker shows Playing."""
        self.sessions.begin(game)
        self.toasts.add_toast(Adw.Toast(title=f"Playing {game.name}"))

    def _epic_playtime_exit(self, game: Game) -> None:
        """The Epic-launched process ended: accumulate local playtime (offline,
        like GOG) and revert the Playing state."""
        started = self.sessions.elapsed()
        if game.id is not None:
            self._downloads.pop(game.id, None)
        self.sessions.end()
        if started:
            from vitrine.domain.entry import entry_for

            entry_for(game, self).record_exit(started / 3600.0, None, self.library)
        self._refresh_running_state()
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"{game.name} closed"))

    def stop_epic(self, game: Game) -> None:
        """Force-stop an Epic game under legendary: kill the tracked job/proc."""
        job = self._downloads.get(game.id) if game.id is not None else None
        from vitrine.services.downloads import DownloadJob

        if isinstance(job, DownloadJob):
            job.stop()
        elif job is not None and hasattr(job, "terminate"):
            self._stop_process(job)
        self.toasts.add_toast(Adw.Toast(title=f"Stopping {game.name}"))

    @staticmethod
    def _stop_process(proc) -> None:
        """SIGTERM then SIGKILL a child process tree."""
        from vitrine.infra import procwatch

        procwatch.terminate_tree(proc.pid)
        import time as _time

        _time.sleep(1.0)
        if proc.poll() is None:
            procwatch.kill_tree(proc.pid)

    def _on_steam_game_exited(self, game: Game) -> None:
        """The Steam-launched process is gone: stop the session and reconcile
        playtime (the entry reads Steam's authoritative value on exit)."""
        if self.steam_watcher is not None:
            self.steam_watcher.stop()
        self.steam_watcher = None
        self.sessions.end()
        self._refresh_running_state()
        self.toasts.add_toast(Adw.Toast(title=f"{game.name} closed"))
        from vitrine.domain.entry import entry_for

        entry_for(game, self).record_exit(0.0, None, self.library)

    def stop_steam(self, game: Game) -> None:
        """Force-stop a Steam game: SIGTERM, then SIGKILL if it ignores it."""
        import time as _time

        from vitrine.infra import steamwatch

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

        self._ticker = GLib.timeout_add_seconds(1, tick)

    def _refresh_running_state(self) -> None:
        """Push the current running state to every tile and the hero bar.

        Always refreshes the detail/hero button so a session that ended (e.g. a
        Steam game quit from the game's own menu, leaving no running game) resets
        its label from "Playing · …" back to "Play".
        """
        game = self.sessions.running_game
        elapsed = self.sessions.elapsed() if game is not None else None
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