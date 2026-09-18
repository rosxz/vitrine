"""The game detail bar: a horizontal hero panel sitting above the library grid.

It mirrors the wide-backdrop treatment of Steam/Galaxy hero rows: the selected
game's banner (falling back to its cover behind a scrim, then initials) fills a
panel with the title, a prominent rectangular play button, playtime and
last-played.

The collapse toggle is an overlay on the hero itself (transparent until
hovered), so it occupies no layout space when idle. Dragging the toggle shrinks
the panel down from its locked default height; clicking it collapses to a thin
strip that keeps the chevron visible so it can be reopened.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gtk

from ..library import Game
from ..util import format_lastplayed, human_playtime, initials

#: The locked default (and maximum) height: 66% of the previous 200px default so
#: the panel takes less room from the game list. Dragging may only shrink below
#: this.
DEFAULT_HEIGHT = 132
MIN_HEIGHT = 90
COLLAPSED_HEIGHT = 26


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class GameDetailBar(Gtk.Box):
    """Collapsible hero detail panel for the currently-selected game."""

    def __init__(
        self,
        on_play: Callable[[Game | None], None] | None = None,
        on_settings: Callable[[Game | None], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_play = on_play
        self._on_settings = on_settings
        self._game: Game | None = None
        self._expanded = True
        self._height = DEFAULT_HEIGHT
        self._dragging = False
        self._drag_start_height = DEFAULT_HEIGHT

        self.set_css_classes(["vitrine-detail"])

        self._body = self._build_body()
        self.append(self._body)

        self._set_height(self._height)
        self.set_visible(False)

    def _build_body(self) -> Gtk.Widget:
        self._backdrop = Gtk.Picture()
        self._backdrop.set_content_fit(Gtk.ContentFit.COVER)
        self._backdrop.set_can_shrink(True)
        self._backdrop.add_css_class("vitrine-detail-backdrop")

        self._placeholder = Gtk.Label()
        self._placeholder.add_css_class("title-1")
        self._placeholder.add_css_class("dim-label")

        scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scrim.set_vexpand(True)
        scrim.add_css_class("vitrine-detail-scrim")

        self._title = Gtk.Label(halign=Gtk.Align.START)
        self._title.add_css_class("vitrine-detail-title")

        self._meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self._meta.set_halign(Gtk.Align.START)
        self._meta.add_css_class("vitrine-detail-sub")

        self._playtime_icon = Gtk.Image.new_from_icon_name("av-symbolic")
        self._playtime_icon.add_css_class("vitrine-detail-metaicon")
        self._playtime_label = Gtk.Label()
        self._meta.append(self._playtime_icon)
        self._meta.append(self._playtime_label)

        self._lastplayed_icon = Gtk.Image.new_from_icon_name("appointment-soon-symbolic")
        self._lastplayed_icon.add_css_class("vitrine-detail-metaicon")
        self._lastplayed_label = Gtk.Label()
        self._meta.append(self._lastplayed_icon)
        self._meta.append(self._lastplayed_label)

        self._play_button = Gtk.Button(label="Play")
        self._play_button.add_css_class("vitrine-play")
        self._play_button.connect("clicked", lambda _b: self._on_play(self._game))

        self._settings_button = Gtk.Button()
        self._settings_button.set_icon_name("emblem-system-symbolic")
        self._settings_button.set_tooltip_text("Game settings")
        self._settings_button.add_css_class("flat")
        self._settings_button.connect("clicked", lambda _b: self._on_settings(self._game))

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        controls.set_halign(Gtk.Align.END)
        controls.set_valign(Gtk.Align.END)
        controls.set_margin_bottom(12)
        controls.set_margin_end(18)
        controls.append(self._settings_button)
        controls.append(self._play_button)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        text.set_halign(Gtk.Align.START)
        text.set_valign(Gtk.Align.END)
        text.set_margin_bottom(18)
        text.set_margin_start(18)
        text.append(self._title)
        text.append(self._meta)

        self._chevron_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._chevron_icon.add_css_class("vitrine-detail-toggle-icon")

        self._toggle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._toggle.add_css_class("vitrine-detail-toggle")
        self._toggle.set_halign(Gtk.Align.CENTER)
        self._toggle.set_valign(Gtk.Align.END)
        self._toggle.set_vexpand(False)
        self._toggle.append(self._chevron_icon)
        self._toggle.set_margin_bottom(6)

        # Clicking the toggle collapses/expands; dragging (a press that moves)
        # shrinks the hero down from its locked default height.
        click = Gtk.GestureClick()
        click.connect("released", self._on_toggle_released)
        self._toggle.add_controller(click)

        self._drag = Gtk.GestureDrag()
        self._drag.connect("drag-begin", self._on_drag_begin)
        self._drag.connect("drag-update", self._on_drag_update)
        self._drag.connect("drag-end", self._on_drag_end)
        self._toggle.add_controller(self._drag)

        # Overlays that describe the game; hidden while collapsed so only the
        # backdrop scrim and the toggle chevron remain in the thin strip.
        self._content_overlays = [self._placeholder, scrim, text, controls]

        overlay = Gtk.Overlay()
        overlay.set_child(self._backdrop)
        for widget in (self._placeholder, scrim, text, controls):
            overlay.add_overlay(widget)
        overlay.add_overlay(self._toggle)
        self._backdrop_overlay = overlay
        return overlay

    # -- public API -----------------------------------------------------------

    def game(self) -> Game | None:
        """The game currently shown by the bar."""
        return self._game

    def set_game(self, game: Game | None) -> None:
        """Show the given game's details, expanding if not already visible."""
        self._game = game
        if game is None:
            self.set_visible(False)
            return
        self.set_visible(True)
        self._title.set_text(game.name)

        banner = game.banner
        cover = game.cover
        if banner or cover:
            self._backdrop.set_filename(banner or cover or "")
            self._backdrop.set_visible(True)
            self._placeholder.set_visible(False)
        else:
            self._backdrop.set_paintable(None)
            self._backdrop.set_visible(False)
            self._placeholder.set_text(initials(game.name))
            self._placeholder.set_visible(True)

        if not self._expanded:
            self._set_expanded(True)
        self._refresh_meta()

    def set_running(self, elapsed_seconds: float | None) -> None:
        """Reflect the currently-running state on the play button."""
        self._refresh_meta()
        if self._game is not None:
            if elapsed_seconds is None:
                self._play_button.set_label("Play")
            else:
                self._play_button.set_label(f"Playing · {human_playtime(elapsed_seconds / 3600.0)}")

    def set_settings_available(self, available: bool) -> None:
        self._settings_button.set_sensitive(available)

    # -- internals ------------------------------------------------------------

    def _refresh_meta(self) -> None:
        if self._game is None:
            return
        self._playtime_label.set_text(human_playtime(self._game.playtime) or "Not played")
        self._lastplayed_label.set_text(format_lastplayed(self._game.lastplayed))

    def _on_toggle_released(self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if not self._dragging:
            self._set_expanded(not self._expanded)

    def _on_drag_begin(self, _drag: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        self._dragging = True
        self._drag_start_height = max(self._height, MIN_HEIGHT)

    def _on_drag_update(self, _drag: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        # Dragging only shrinks the panel down towards MIN_HEIGHT; it can never
        # grow past the locked default height.
        new_height = self._drag_start_height + int(offset_y)
        self._set_expanded(True)
        self._set_height(clamp(new_height, MIN_HEIGHT, DEFAULT_HEIGHT))

    def _on_drag_end(self, _drag: Gtk.GestureDrag, offset_x: float, offset_y: float) -> None:
        self._dragging = False

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._chevron_icon.set_from_icon_name(
            "pan-up-symbolic" if expanded else "pan-down-symbolic"
        )
        for widget in self._content_overlays:
            widget.set_visible(expanded)
        if not expanded:
            self._body.set_size_request(-1, COLLAPSED_HEIGHT)
        else:
            self._body.set_size_request(-1, max(self._height, MIN_HEIGHT))

    def _set_height(self, height: int) -> None:
        self._height = clamp(height, MIN_HEIGHT, DEFAULT_HEIGHT)
        self._body.set_size_request(-1, self._height)