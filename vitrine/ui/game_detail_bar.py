"""The game detail bar: a horizontal hero panel sitting above the library grid.

It mirrors the wide-backdrop treatment of Steam/Galaxy hero rows: the selected
game's banner (falling back to its cover behind a scrim, then initials) fills a
panel with the title, a prominent rectangular play button, playtime and
last-played. A chevron and a clickable handle collapse the panel down to a thin
strip.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gtk

from ..library import Game
from ..util import format_lastplayed, human_playtime, initials

DETAIL_HEIGHT = 200
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

        self._handle = Gtk.Button()
        self._handle.add_css_class("vitrine-detail-handle")
        self._handle.set_halign(Gtk.Align.FILL)
        self._handle.set_valign(Gtk.Align.CENTER)
        self._handle.set_vexpand(False)
        self._handle.connect("clicked", self._on_handle_clicked)
        self._chevron_icon = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._handle.set_child(self._chevron_icon)
        self.append(self._handle)

        self._body = self._build_body()
        self.append(self._body)

        self._set_height(self._expanded)
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
        controls.set_margin_bottom(14)
        controls.set_margin_end(18)
        controls.append(self._settings_button)
        controls.append(self._play_button)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        text.set_halign(Gtk.Align.START)
        text.set_valign(Gtk.Align.END)
        text.set_margin_bottom(20)
        text.set_margin_start(18)
        text.append(self._title)
        text.append(self._meta)

        self._backdrop_overlay = Gtk.Overlay()
        self._backdrop_overlay.set_child(self._backdrop)
        self._backdrop_overlay.add_overlay(self._placeholder)
        self._backdrop_overlay.add_overlay(scrim)
        self._backdrop_overlay.add_overlay(text)
        self._backdrop_overlay.add_overlay(controls)

        self._backdrop_overlay.set_size_request(-1, DETAIL_HEIGHT)
        return self._backdrop_overlay

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

    def _on_handle_clicked(self, _button: Gtk.Button) -> None:
        self._set_expanded(not self._expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._body.set_visible(expanded)
        self._chevron_icon.set_from_icon_name(
            "pan-up-symbolic" if expanded else "pan-down-symbolic"
        )
        self._set_height(DETAIL_HEIGHT if expanded else COLLAPSED_HEIGHT)

    def _set_height(self, height: int) -> None:
        self._body.set_size_request(-1, height)