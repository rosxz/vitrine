"""Main window: source sidebar on the left, library grid on the right."""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from ..library import Game, Library
from ..running import GameAlreadyRunning, Runtime
from ..sources import registry
from .add_game_dialog import AddGameDialog
from .library_view import LibraryView

logger = logging.getLogger(__name__)

ALL_GAMES = "__all__"


class VitrineWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, library: Library) -> None:
        super().__init__(application=application, title="Vitrine")
        self.library = library
        self.current_source: str | None = None

        self.set_default_size(1100, 720)

        self.runtime = Runtime()
        self.runtime.on_start = self._on_game_started
        self.runtime.on_exit = self._on_game_exited

        self.library_view = LibraryView(on_activate=self.on_game_activated)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.library_view)

        content = Adw.ToolbarView()
        content.set_content(self.toasts)
        content.add_top_bar(self._build_header_bar())

        split = Adw.OverlaySplitView()
        split.set_sidebar(self._build_sidebar())
        split.set_content(content)
        split.set_min_sidebar_width(210)
        split.set_max_sidebar_width(320)
        self.set_content(split)

        self._ticker: int | None = None
        self.setup_running_ticker()

        self.reload()

    # -- UI construction -------------------------------------------------------

    def _build_header_bar(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        title = Adw.WindowTitle(title="Vitrine")
        header.set_title_widget(title)
        self.title_widget = title

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a game")
        add_button.connect("clicked", self.on_add_game_clicked)
        header.pack_end(add_button)

        return header

    def _build_sidebar(self) -> Gtk.Widget:
        self.source_list = Gtk.ListBox()
        self.source_list.add_css_class("navigation-sidebar")
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
        scroller.set_margin_top(6)
        scroller.set_margin_bottom(6)
        scroller.set_margin_start(6)
        scroller.set_margin_end(6)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sources"))

        sidebar = Adw.ToolbarView()
        sidebar.add_top_bar(header)
        sidebar.set_content(scroller)
        return sidebar

    def _add_source_row(self, source_id: str, title: str, icon_name: str) -> None:
        row = Gtk.ListBoxRow()
        row.source_id = source_id  # type: ignore[attr-defined]

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
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

    def on_add_game_clicked(self, _button: Gtk.Button) -> None:
        dialog = AddGameDialog(on_add=self.on_game_added)
        dialog.present(self)

    def on_game_added(self, game: Game) -> None:
        self.library.add(game)
        self.reload()
        self.toasts.add_toast(Adw.Toast(title=f"Added {game.name}"))

    def on_game_activated(self, game: Game) -> None:
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

    def _stop_game(self) -> None:
        game = self.runtime.running_game
        self.runtime.stop()
        if game is not None:
            self.toasts.add_toast(Adw.Toast(title=f"Stopping {game.name}"))

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
