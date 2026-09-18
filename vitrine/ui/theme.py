"""Theming: a small registry of visual themes and a manager that applies them.

Every theme shares the same layout grammar (page structure, positions, portrait
art, the detail bar); only surface styling -- accent, corners, shapes, spacing
-- differs between themes. Themes are shipped as CSS files that a single
application-wide :class:`GtkCssProvider` loads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

from gi.repository import Adw, Gdk, Gtk

#: Default theme used when no setting has been stored yet.
DEFAULT_THEME = "galaxy"


@dataclass(frozen=True)
class Theme:
    """Metadata about a theme; the styling itself lives in its CSS file."""

    id: str
    name: str
    description: str


#: Themes keyed by id. ``galaxy`` (a GOG-Galaxy-inspired dark pattern) is the
#: default; ``system`` follows the desktop's light/dark colour preference but
#: keeps the same layout grammar.
THEMES: dict[str, Theme] = {
    "galaxy": Theme(
        id="galaxy",
        name="Galaxy",
        description="Dark, GOG Galaxy-inspired accent and spacing",
    ),
    "system": Theme(
        id="system",
        name="Follow system",
        description="Neutral styling that follows the desktop theme",
    ),
}


def theme_ids() -> list[str]:
    return list(THEMES)


def is_theme(value: str | None) -> bool:
    return value in THEMES


def resolve_theme(value: Any, default: str = DEFAULT_THEME) -> str:
    """Return ``value`` if it names a known theme, else ``default``."""
    return value if isinstance(value, str) and is_theme(value) else default


class ThemeManager:
    """Loads and applies the active theme's CSS application-wide.

    A single :class:`Gtk.CssProvider` is reused so that switching themes never
    leaks providers or priority levels. Callers can subscribe to a
    ``changed``-style callback to refresh widgets that cache theme-derived
    state.
    """

    def __init__(self) -> None:
        self._provider: Gtk.CssProvider | None = None
        self._theme = DEFAULT_THEME
        self.on_changed: Callable[[str], None] | None = None

    @property
    def theme(self) -> str:
        return self._theme

    def apply(self, theme: str) -> str:
        """Set and install the given theme's CSS. Returns the active theme id."""
        theme = resolve_theme(theme)
        if theme == self._theme and self._provider is not None:
            return theme

        provider = Gtk.CssProvider()
        css = resources.files("vitrine.ui.style").joinpath(f"{theme}.css").read_text()
        provider.load_from_string(css)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        # Galaxy is a fixed dark palette; the system theme follows the desktop's
        # light/dark preference.
        style_manager = Adw.StyleManager.get_default()
        color_scheme = (
            Adw.ColorScheme.FORCE_DARK
            if theme == "galaxy"
            else Adw.ColorScheme.DEFAULT
        )
        if style_manager.get_color_scheme() != color_scheme:
            style_manager.set_color_scheme(color_scheme)

        self._provider = provider
        self._theme = theme
        if self.on_changed is not None:
            self.on_changed(theme)
        return theme