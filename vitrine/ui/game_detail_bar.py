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
from importlib import resources

from gi.repository import Gtk

from vitrine.infra.util import format_lastplayed, human_playtime, initials
from vitrine.services.library import Game

#: Fixed hero height. Kept deliberately compact so the panel leaves room for the
#: game list.
DEFAULT_HEIGHT = 200
COLLAPSED_HEIGHT = 26

#: Side of the square icon buttons in the hero control cluster (settings, store,
#: favorite, achievements). Keeps their widths uniform so the equal spacing looks
#: even; a bare emoji label would otherwise be wider than the icon buttons.
_ICON_BUTTON = 34


def _brand_icon_path(name: str) -> str:
    """Absolute path to a bundled brand/art SVG under ``vitrine/ui/style/brand``."""
    return str(resources.files("vitrine.ui.style").joinpath("brand", name))


class GameDetailBar(Gtk.Box):
    """Collapsible hero detail panel for the currently-selected game."""

    def __init__(
        self,
        on_play: Callable[[Game | None], None] | None = None,
        on_settings: Callable[[Game | None], None] | None = None,
        on_favorite: Callable[[Game | None], None] | None = None,
        on_cancel: Callable[[Game | None], None] | None = None,
        on_store: Callable[[Game | None], None] | None = None,
        on_achievements: Callable[[Game | None], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_play = on_play
        self._on_settings = on_settings
        self._on_favorite = on_favorite
        self._on_cancel = on_cancel
        self._on_store = on_store
        self._on_achievements = on_achievements
        self._game: Game | None = None
        self._expanded = True
        self._downloading = False

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

        # Playtime now uses the icon formerly used for last-played.
        self._playtime_icon = Gtk.Image.new_from_icon_name("appointment-soon-symbolic")
        self._playtime_icon.add_css_class("vitrine-detail-metaicon")
        self._playtime_label = Gtk.Label()
        self._meta.append(self._playtime_icon)
        self._meta.append(self._playtime_label)

        # Last-played uses the bundled calendar brand mark.
        self._lastplayed_icon = Gtk.Image.new_from_file(_brand_icon_path("calendar.svg"))
        self._lastplayed_icon.set_pixel_size(16)
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

        # Open the game's store page in the browser (Steam/GOG/Epic store). Uses
        # the bundled explore mark; hidden when the game has no store page (local).
        self._store_button = Gtk.Button()
        self._store_icon = Gtk.Image.new_from_file(_brand_icon_path("explore-svgrepo-com.svg"))
        self._store_icon.set_pixel_size(20)
        self._store_button.set_child(self._store_icon)
        self._store_button.set_tooltip_text("Open store page")
        self._store_button.add_css_class("flat")
        self._store_button.set_visible(False)
        self._store_button.connect("clicked", lambda _b: self._on_store(self._game))

        # Achievement button: a trophy emoji that opens the achievements viewer. It's
        # given a fixed pixel size so it sits as a compact square icon like the
        # other flat buttons, keeping even spacing (a bare emoji label is wider).
        self._achievements_button = Gtk.Button(label="🏆")
        self._achievements_button.set_tooltip_text("View achievements")
        self._achievements_button.add_css_class("flat")
        self._achievements_button.set_size_request(_ICON_BUTTON, _ICON_BUTTON)
        self._achievements_button.set_visible(False)
        self._achievements_button.connect("clicked", lambda _b: self._on_achievements(self._game))

        # Favorite star: the bundled brand SVG (filled with the play-button accent)
        # is semi-transparent when not starred and fully opaque when starred.
        self._favorite_button = Gtk.Button()
        self._favorite_button.add_css_class("flat")
        self._favorite_button.add_css_class("vitrine-star")
        self._favorite_button.set_tooltip_text("Add to favorites")
        self._favorite_icon = Gtk.Image.new_from_file(_brand_icon_path("favorite.svg"))
        self._favorite_icon.set_pixel_size(22)
        self._favorite_button.set_child(self._favorite_icon)
        self._favorite_button.connect("clicked", lambda _b: self._on_favorite(self._game))

        self._cancel_button = Gtk.Button()
        self._cancel_button.set_icon_name("process-stop-symbolic")
        self._cancel_button.set_tooltip_text("Cancel download")
        self._cancel_button.add_css_class("vitrine-cancel")
        self._cancel_button.set_visible(False)
        self._cancel_button.connect("clicked", lambda _b: self._on_cancel(self._game))

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        controls.set_halign(Gtk.Align.END)
        controls.set_valign(Gtk.Align.END)
        controls.set_margin_bottom(12)
        controls.set_margin_end(18)
        controls.append(self._favorite_button)
        controls.append(self._store_button)
        controls.append(self._achievements_button)
        controls.append(self._settings_button)
        controls.append(self._play_button)
        controls.append(self._cancel_button)

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
        self._refresh_favorite()

    def _refresh_favorite(self) -> None:
        if self._game is None:
            return
        starred = bool(self._game.favorite)
        self._favorite_button.set_tooltip_text(
            "Remove from favorites" if starred else "Add to favorites"
        )
        if starred:
            self._favorite_button.add_css_class("favorited")
        else:
            self._favorite_button.remove_css_class("favorited")

    def set_favorite(self, favorite: bool) -> None:
        """Refresh the star to match a (possibly externally-updated) favorite state."""
        if self._game is not None:
            self._game.favorite = favorite
        self._refresh_favorite()

    def set_running(self, elapsed_seconds: float | None) -> None:
        """Reflect the currently-running state on the play button.

        The label is only overridden for a running session; while a game is
        downloading (``_downloading``), the ticker must not clobber its
        "Downloading…" label back to "Play".
        """
        self._refresh_meta()
        if self._game is not None and not self._downloading:
            if elapsed_seconds is None:
                self._play_button.set_label("Play")
            else:
                self._play_button.set_label(f"Playing · {human_playtime(elapsed_seconds / 3600.0)}")

    def set_settings_available(self, available: bool) -> None:
        self._settings_button.set_sensitive(available)

    def set_store_visible(self, visible: bool) -> None:
        """Show/hide the "open store page" button (only store-sourced games)."""
        self._store_button.set_visible(visible)

    def set_achievements(
        self,
        total: int | None,
        unlocked: int | None,
        available: bool = False,
        on_click: Callable[[Game | None], None] | None = None,
    ) -> None:
        """Show/hide the trophy button.

        ``total``/``unlocked`` come from the game's cached summary (may be
        unknown -> ``None``). ``available`` is whether the game can have
        achievements at all (it has an achievement provider); when true the
        button is always shown so the viewer is reachable, even before a fetch.
        The button is icon-only; the count lives in its tooltip.
        """
        if on_click is not None:
            self._on_achievements = on_click
        if not available and not total:
            self._achievements_button.set_visible(False)
            return
        if total:
            tip = f"View achievements ({unlocked or 0}/{total})"
        else:
            tip = "View achievements"
        self._achievements_button.set_tooltip_text(tip)
        self._achievements_button.set_visible(True)

    def set_downloading(self, downloading: bool) -> None:
        """Lock and recolor the play button while this game is downloading.

        While downloading, a cancel button replaces the play action; the
        settings and favorite buttons stay enabled so the user isn't fully
        locked out of the detail bar.
        """
        self._downloading = downloading
        for widget in (self._play_button, self._settings_button):
            widget.set_sensitive(not downloading)
        if downloading:
            self._play_button.set_label("Downloading…")
            self._play_button.add_css_class("vitrine-downloading")
        else:
            self._play_button.set_label("Play")
            self._play_button.remove_css_class("vitrine-downloading")
        self._cancel_button.set_visible(downloading)

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