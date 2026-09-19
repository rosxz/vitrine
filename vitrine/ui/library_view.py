"""The library grid.

Every tile uses the same 2:3 portrait box and cover-fits whatever artwork the
source provides, so a Steam capsule, a GOG tile and a local game's placeholder
all occupy identical space. That is the whole point of the view. Selecting a
tile raises the ``selection-changed`` signal so the window can update the
detail bar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from gi.repository import Adw, GObject, Gtk

from ..library import Game
from ..util import human_playtime, initials

#: Max lines a game name spans before it is truncated with an ellipsis.
NAME_MAX_LINES = 2
#: Approximate height of one name line, used to reserve the name area so
#: content never changes tile height.
NAME_LINE_HEIGHT = 16

#: Portrait cover ratio (width / height), matching Steam's library capsules.
COVER_RATIO = 2 / 3
COVER_WIDTH = 180
#: Fixed tile height = cover (COVER_WIDTH / ratio) + a name area sized for up to
#: NAME_MAX_LINES lines, so every tile -- whatever the source, title length or
#: how few games are shown -- keeps the same dimensions instead of the grid
#: stretching a lone tile to fill the pane or a long title stretching the tile.
TILE_HEIGHT = int(COVER_WIDTH / COVER_RATIO) + NAME_MAX_LINES * NAME_LINE_HEIGHT
MIN_COLUMNS = 2
MAX_COLUMNS = 9


class GameTile(Gtk.FlowBoxChild):
    """A single game: cover box, source badge, name."""

    def __init__(self, game: Game) -> None:
        super().__init__()
        self.game = game
        self.add_css_class("vitrine-tile")
        if not game.installed and game.source == "steam":
            self.add_css_class("not-installed")
        self._context_callback: Callable[[Game, float, float], None] | None = None

        gesture = Gtk.GestureClick()
        gesture.set_button(3)  # GDK_BUTTON_SECONDARY (right mouse button)
        gesture.connect("pressed", self._on_secondary_pressed)
        self.add_controller(gesture)

        self.cover = Gtk.Picture()
        self.cover.set_content_fit(Gtk.ContentFit.COVER)
        self.cover.set_can_shrink(True)

        placeholder = Gtk.Label(label=initials(game.name))
        placeholder.add_css_class("title-1")
        placeholder.add_css_class("dim-label")

        overlay = Gtk.Overlay()
        overlay.set_child(placeholder)
        overlay.add_overlay(self.cover)

        if game.source and game.source != "local":
            badge = Gtk.Label(label=game.source.capitalize())
            badge.add_css_class("caption")
            badge.add_css_class("vitrine-badge")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            badge.set_margin_start(6)
            badge.set_margin_top(6)
            overlay.add_overlay(badge)

        frame = Gtk.AspectFrame(ratio=COVER_RATIO, xalign=0.5, yalign=0.5, obey_child=False)
        frame.set_obey_child(False)
        frame.set_child(overlay)
        frame.set_size_request(COVER_WIDTH, int(COVER_WIDTH / COVER_RATIO))

        self.running_dot = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        self.running_label = Gtk.Label(label="")
        running = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        running.append(self.running_dot)
        running.append(self.running_label)
        running.set_halign(Gtk.Align.START)
        running.set_valign(Gtk.Align.END)
        running.set_margin_start(6)
        running.set_margin_bottom(6)
        running.set_visible(False)
        overlay.add_overlay(running)
        self.running_footer = running

        name = Gtk.Label(label=game.name, wrap=True, justify=Gtk.Justification.CENTER, lines=NAME_MAX_LINES)
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END (truncates with '…')
        # Cap the wrap width so a long name wraps within the cover instead of
        # widening the tile (which used to unbalance the cross-source grid).
        name.set_max_width_chars(COVER_WIDTH // 8)
        name.add_css_class("vitrine-tile-name")
        if not (game.cover or game.banner):
            name.add_css_class("dim")

        # A Gtk.Label derives its natural height from however many lines its
        # text wraps to, so a flowing-box row would grow for 2-line titles.
        # Put the label as an overlay over a fixed-size background: the overlay
        # takes its height from the background, so the tile's height never
        # changes; longer titles still render NAME_MAX_LINES lines (clipped,
        # ellipsized with '…') without stretching the row.
        name_bg = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name_bg.set_size_request(COVER_WIDTH, NAME_MAX_LINES * NAME_LINE_HEIGHT)
        name_bg.set_valign(Gtk.Align.FILL)
        name_bg.set_vexpand(False)
        name_area = Gtk.Overlay()
        name_area.set_child(name_bg)
        name_area.add_overlay(name)
        name_area.set_overflow(Gtk.Overflow.HIDDEN)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(frame)
        box.append(name_area)
        # Rigid width + height so content can never stretch the tile; the name
        # stays uniform across sources and title lengths.
        box.set_size_request(COVER_WIDTH, TILE_HEIGHT)
        self.set_child(box)

        # Fix the tile's total size and stop it expanding, so the grid never
        # stretches a lone tile to fill the pane (which made Local look huge
        # versus populated Steam/All views).
        self.set_size_request(COVER_WIDTH, TILE_HEIGHT)
        self.set_hexpand(False)
        self.set_vexpand(False)

    def set_cover(self, path: str | None) -> None:
        """Show a cover file, or fall back to the initials placeholder."""
        if path:
            self.cover.set_filename(path)
        else:
            self.cover.set_paintable(None)

    def set_running(self, elapsed_seconds: float | None) -> None:
        """Show or hide the running indicator on the cover."""
        if elapsed_seconds is None:
            self.running_footer.set_visible(False)
            self.running_label.set_text("")
            return
        self.running_label.set_text(human_playtime(elapsed_seconds / 3600.0))
        self.running_footer.set_visible(True)

    def set_context_callback(self, callback: Callable[[Game, float, float], None]) -> None:
        """Call ``callback(game, x, y)`` on a right-click over this tile.

        (x, y) are the click coordinates relative to this tile, useful for
        anchoring a context menu.
        """
        self._context_callback = callback

    def _on_secondary_pressed(self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if self._context_callback is not None:
            self._context_callback(self.game, float(x), float(y))


class LibraryView(Gtk.Stack):
    """Scrolling grid of games, with an empty state and a selection signal."""

    def __init__(
        self,
        on_activate: Callable[[Game], None],
        on_context: Callable[[Game, float, float], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_activate = on_activate
        self._on_context = on_context or (lambda _game, _x, _y: None)

        self.flow = Gtk.FlowBox()
        # Tiles keep their own fixed pixel width (hence not homogeneous), so
        # short vs long names never stretch the grid; they wrap in columns.
        self.flow.set_homogeneous(False)
        self.flow.set_min_children_per_line(MIN_COLUMNS)
        self.flow.set_max_children_per_line(MAX_COLUMNS)
        # Pack tiles at their fixed natural size (left-aligned) rather than
        # stretching them to fill the pane; keeps sizes consistent across
        # views regardless of how many games are shown.
        self.flow.set_halign(Gtk.Align.START)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # Activation must be a deliberate double-click (or Enter/Space), not a
        # single click: GTK's default is activate-on-single-click, which would
        # launch a game the moment it is merely selected.
        self.flow.set_activate_on_single_click(False)
        self.flow.set_row_spacing(12)
        self.flow.set_column_spacing(12)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_margin_top(18)
        self.flow.set_margin_bottom(18)
        self.flow.set_margin_start(18)
        self.flow.set_margin_end(18)
        self.flow.connect("child-activated", self._on_child_activated)
        self.flow.connect("selected-children-changed", self._on_selection_changed)

        # A click on the grid background (not on a tile) clears the selection,
        # which hides the detail bar.
        background_click = Gtk.GestureClick()
        background_click.set_button(1)
        background_click.connect("pressed", self._on_background_pressed)
        self.flow.add_controller(background_click)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.flow)
        scroller.set_vexpand(True)

        self.empty = Adw.StatusPage()
        self.empty.set_icon_name("applications-games-symbolic")
        self.empty.set_title("No games yet")
        self.empty.set_description("Games you add will appear here.")

        self.add_named(scroller, "grid")
        self.add_named(self.empty, "empty")
        self.set_visible_child_name("empty")

    # -- public API -----------------------------------------------------------

    def set_games(self, games: Iterable[Game]) -> None:
        """Replace the contents of the grid."""
        while child := self.flow.get_first_child():
            self.flow.remove(child)

        count = 0
        for game in games:
            tile = GameTile(game)
            tile.set_context_callback(self._on_context)
            self.flow.append(tile)
            count += 1

        self.set_visible_child_name("grid" if count else "empty")
        if count:
            self.flow.select_child(self.flow.get_first_child())

    def selected_game(self) -> Game | None:
        selected = self.flow.get_selected_children()
        if not selected:
            return None
        child = selected[0]
        return child.game if isinstance(child, GameTile) else None

    # -- internals ------------------------------------------------------------

    def _on_child_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, GameTile):
            self._on_activate(child.game)

    def _on_background_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Clear the selection when the click was on the grid, not a tile."""
        if n_press > 1:
            return
        picked = self.flow.pick(int(x), int(y), Gtk.PickFlags(0))
        if picked is None or not _within_tile(picked):
            self.flow.unselect_all()

    def _on_selection_changed(self, flow: Gtk.FlowBox) -> None:
        selected = _selected(flow)
        for child in _children(flow):
            child.set_css_classes(
                ["vitrine-tile", "selected"]
                if child is selected
                else ["vitrine-tile"]
            )
        self.emit("selection-changed")


def _children(flow: Gtk.FlowBox):
    child = flow.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _within_tile(widget: Gtk.Widget) -> bool:
    """True if ``widget`` is a GameTile or a descendant of one."""
    current: Gtk.Widget | None = widget
    while current is not None:
        if isinstance(current, GameTile):
            return True
        current = current.get_parent()
    return False


def _selected(flow: Gtk.FlowBox) -> Gtk.FlowBoxChild | None:
    selected = flow.get_selected_children()
    return selected[0] if selected else None


GObject.type_register(LibraryView)
GObject.signal_new(
    "selection-changed",
    LibraryView,
    GObject.SignalFlags.RUN_FIRST,
    GObject.TYPE_NONE,
    (),
)