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

#: Portrait cover ratio (width / height), matching Steam's library capsules.
COVER_RATIO = 2 / 3
COVER_WIDTH = 180
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

        name = Gtk.Label(label=game.name, wrap=True, justify=Gtk.Justification.CENTER, lines=2)
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END
        name.add_css_class("vitrine-tile-name")
        if not (game.cover or game.banner):
            name.add_css_class("dim")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(frame)
        box.append(name)
        # Constrain the whole tile to the cover width so a long name wraps
        # at that width instead of widening the tile and unbalancing the
        # cross-source homogeneous grid.
        box.set_size_request(COVER_WIDTH, -1)
        self.set_child(box)

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
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(MIN_COLUMNS)
        self.flow.set_max_children_per_line(MAX_COLUMNS)
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