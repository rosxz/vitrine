"""The game detail bar: a horizontal hero panel sitting above the library grid.

It mirrors the wide-backdrop treatment of Steam/Galaxy hero rows: the selected
game's banner (falling back to its cover behind a scrim, then initials) fills a
panel with the title, a prominent rectangular play button, playtime and
last-played.

The collapse toggle is an overlay on top of the hero (transparent until
hovered), so it occupies no layout space when idle. Clicking it collapses the
panel down to a thin strip that keeps the chevron visible so it can be
reopened.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gtk

from ..library import Game
from ..util import format_lastplayed, human_playtime, initials

#: Fixed hero height. Kept deliberately compact so the panel leaves room for the
#: game list.
DEFAULT_HEIGHT = 200
COLLAPSED_HEIGHT = 26


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

        self.set_css_classes(["vitrine-detail"])

        self._body = self._build_body()
        self.append(self._body)

        self._set_expanded(self._expanded)
        self.set_visible(False)

    def _build_body(self) -> Gtk.Widget:
        # The scrim is the overlay's main child and dictates the fixed height;
        # the backdrop picture sits on top of it, filling the box and cropping
        # its artwork (cover-fit) so the hero never grows beyond the height.
        scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scrim.set_vexpand(True)
        scrim.set_hexpand(True)
        scrim.add_css_class("vitrine-detail-scrim")

        self._backdrop = Gtk.Picture()
        self._backdrop.set_content_fit(Gtk.ContentFit.COVER)
        self._backdrop.set_can_shrink(True)
        self._backdrop.set_halign(Gtk.Align.FILL)
        self._backdrop.set_valign(Gtk.Align.FILL)
        self._backdrop.set_hexpand(True)
        self._backdrop.set_vexpand(False)
        self._backdrop.add_css_class("vitrine-detail-backdrop")

        self._placeholder = Gtk.Label()
        self._placeholder.add_css_class("title-1")
        self._placeholder.add_css_class("dim-label")

        self._title = Gtk.Label(halign=Gtk.Align.START, wrap=True, lines=2)
        self._title.set_ellipsize(3)  # Pango.EllipsizeMode.END (truncates with '…')
        self._title.set_max_width_chars(40)
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

        # The collapse chevron sits on top of the hero (top-center), floating
        # over the artwork and invisible until hovered.
        self._chevron_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._chevron_icon.add_css_class("vitrine-detail-toggle-icon")

        self._toggle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._toggle.add_css_class("vitrine-detail-toggle")
        self._toggle.set_halign(Gtk.Align.CENTER)
        self._toggle.set_valign(Gtk.Align.START)
        self._toggle.set_vexpand(False)
        self._toggle.append(self._chevron_icon)
        self._toggle.set_margin_top(6)

        click = Gtk.GestureClick()
        click.connect("released", self._on_toggle_released)
        self._toggle.add_controller(click)

        # A semi-translucent dark panel behind the title/playtime/play controls
        # so text keeps contrast regardless of what the banner shows underneath.
        # It spans the full width and reaches the bottom edge of the hero.
        shade = Gtk.Box()
        shade.set_hexpand(True)
        shade.set_valign(Gtk.Align.END)
        shade.set_size_request(-1, 96)
        shade.add_css_class("vitrine-detail-shade")
        self._shade = shade

        # Overlays that describe the game; hidden while collapsed so only the
        # toggle chevron remains in the thin strip.
        self._content_overlays = [
            self._backdrop,
            self._placeholder,
            scrim,
            shade,
            text,
            controls,
        ]

        overlay = Gtk.Overlay()
        overlay.set_child(scrim)  # dictates the fixed height
        overlay.add_overlay(self._backdrop)  # cover-crops to fill the scrim
        overlay.add_overlay(self._shade)  # full-bleed dark panel over the art
        for widget in (self._placeholder, text, controls):
            overlay.add_overlay(widget)
        overlay.add_overlay(self._toggle)
        self._backdrop_overlay = overlay
        return overlay

    # -- public API -----------------------------------------------------------

    def game(self) -> Game | None:
        """The game currently shown by the bar."""
        return self._game

    def set_game(self, game: Game | None) -> None:
        """Show the given game's details, keeping the current collapse state.

        Collapsing is sticky: cycling between games will not re-expand a panel
        the user has toggled closed.
        """
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
        self._set_expanded(not self._expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._chevron_icon.set_from_icon_name(
            "pan-up-symbolic" if expanded else "pan-down-symbolic"
        )
        for widget in self._content_overlays:
            widget.set_visible(expanded)
        self._body.set_size_request(
            -1, DEFAULT_HEIGHT if expanded else COLLAPSED_HEIGHT
        )