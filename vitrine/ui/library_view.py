"""The library grid.

Every tile uses the same 2:3 portrait box and cover-fits whatever artwork the
source provides, so a Steam capsule, a GOG tile and a local game's placeholder
all occupy identical space. That is the whole point of the view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from gi.repository import Adw, Gtk

from ..library import Game
from ..util import initials

#: Portrait cover ratio (width / height), matching Steam's library capsules.
COVER_RATIO = 2 / 3
COVER_WIDTH = 180
MIN_COLUMNS = 2
MAX_COLUMNS = 9


class GameTile(Gtk.FlowBoxChild):
    """A single game: cover box, source badge, name."""

    def __init__(self, game: Game, on_activate: Callable[[Game], None]) -> None:
        super().__init__()
        self.game = game
        self._on_activate = on_activate

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
            badge.add_css_class("card")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            badge.set_margin_start(6)
            badge.set_margin_top(6)
            overlay.add_overlay(badge)

        frame = Gtk.AspectFrame(ratio=COVER_RATIO, xalign=0.5, yalign=0.5, obey_child=False)
        frame.set_obey_child(False)
        frame.set_child(overlay)
        frame.set_size_request(COVER_WIDTH, int(COVER_WIDTH / COVER_RATIO))
        frame.add_css_class("card")

        name = Gtk.Label(label=game.name, wrap=True, justify=Gtk.Justification.CENTER, lines=2)
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(frame)
        box.append(name)
        self.set_child(box)

    def set_cover(self, path: str | None) -> None:
        """Show a cover file, or fall back to the initials placeholder."""
        if path:
            self.cover.set_filename(path)
        else:
            self.cover.set_paintable(None)


class LibraryView(Gtk.Stack):
    """Scrolling grid of games, with an empty state."""

    def __init__(self, on_activate: Callable[[Game], None]) -> None:
        super().__init__()
        self._on_activate = on_activate

        self.flow = Gtk.FlowBox()
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(MIN_COLUMNS)
        self.flow.set_max_children_per_line(MAX_COLUMNS)
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_row_spacing(12)
        self.flow.set_column_spacing(12)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_margin_top(18)
        self.flow.set_margin_bottom(18)
        self.flow.set_margin_start(18)
        self.flow.set_margin_end(18)
        self.flow.connect("child-activated", self._on_child_activated)

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

    def set_games(self, games: Iterable[Game]) -> None:
        """Replace the contents of the grid."""
        while child := self.flow.get_first_child():
            self.flow.remove(child)

        count = 0
        for game in games:
            self.flow.append(GameTile(game, self._on_activate))
            count += 1

        self.set_visible_child_name("grid" if count else "empty")

    def _on_child_activated(self, _flow: Gtk.FlowBox, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, GameTile):
            self._on_activate(child.game)
