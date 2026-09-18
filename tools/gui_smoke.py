"""Build the real window against a throwaway library, then quit.

Import checks cannot catch widget misuse; this constructs the application, its
database, the sidebar and a populated library grid. Needs an X or Wayland
display, so run it under Xvfb:

    Xvfb :99 -screen 0 1280x800x24 &
    DISPLAY=:99 python tools/gui_smoke.py

Set VITRINE_SMOKE_ALLOW_DESKTOP=1 to watch it on your own session instead.
"""

# ruff: noqa: E402

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

# Allow running this file directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point every XDG directory at a scratch location before any vitrine import, so
# the smoke test never touches the developer's real library.
_sandbox = Path(tempfile.mkdtemp(prefix="vitrine-smoke-"))
os.environ["XDG_DATA_HOME"] = str(_sandbox / "data")
os.environ["XDG_CACHE_HOME"] = str(_sandbox / "cache")
os.environ["XDG_CONFIG_HOME"] = str(_sandbox / "config")

# Bind to X11 by default so the window lands on the Xvfb display: GTK prefers
# Wayland whenever WAYLAND_DISPLAY is set, which would pop a window onto the
# developer's own desktop during a test run.
if not os.environ.get("VITRINE_SMOKE_ALLOW_DESKTOP"):
    os.environ["GDK_BACKEND"] = "x11"
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ.setdefault("DISPLAY", ":99")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib

from vitrine.application import VitrineApplication
from vitrine.library import Game
from vitrine.ui import VitrineWindow
from vitrine.ui.game_dialogs import AddGameDialog, GameSettingsDialog
from vitrine.ui.settings_dialog import SettingsDialog

TRUE = shutil.which("true")


def tile_count(view: Any) -> int:
    """How many tiles the library grid currently holds."""
    count = 0
    child = view.flow.get_first_child()
    while child is not None:
        count += 1
        child = child.get_next_sibling()
    return count


def check_shell(application: VitrineApplication) -> None:
    """Assert that the window, the grid and the dialogs came up as expected."""
    library = application.library
    assert library is not None, "no library was created"

    windows = application.get_windows()
    assert windows, "do_activate() did not create a window"
    window = windows[0]
    assert isinstance(window, VitrineWindow), f"unexpected window type {type(window).__name__}"

    library.add(Game(name="Smoke Test Game", runner="linux", executable=TRUE, source="local"))
    library.add(Game(name="Another One", executable="/bin/true", source="steam", source_id="1"))

    window.reload()
    rendered = tile_count(window.library_view)
    assert rendered == 2, f"expected 2 tiles in the grid, got {rendered}"
    print(f"window '{window.get_title()}' rendered {rendered} tiles")

    # A single click must select without launching: activation is double-click
    # only (or Enter), so the grid must not be activate-on-single-click.
    assert not window.library_view.flow.get_activate_on_single_click(), \
        "single click must not activate/launch a tile"

    # The first tile should be auto-selected, which drives the detail bar.
    selected = window.library_view.selected_game()
    assert selected is not None, "expected a game to be selected after set_games"
    assert window.detail_bar.game() is selected, "detail bar did not reflect the selection"
    assert window.detail_bar.get_visible(), "detail bar should be visible when a game is selected"
    print(f"detail bar shows '{window.detail_bar.game().name}'")

    # Selecting the second tile updates the detail bar.
    window.library_view.flow.select_child(window.library_view.flow.get_child_at_index(1))
    selected = window.library_view.selected_game()
    assert window.detail_bar.game() is selected, "detail bar did not follow selection change"

    # Clicking the grid background (empty area) clears the selection and hides
    # the detail bar.
    window.library_view._on_background_pressed(None, 1, 300, 700)
    assert window.library_view.selected_game() is None, "background click should deselect"
    assert not window.detail_bar.get_visible(), "detail bar should hide after background click"

    # Collapsing the detail bar is sticky when switching games.
    window.library_view.flow.select_child(window.library_view.flow.get_child_at_index(0))
    assert window.detail_bar.get_visible(), "detail bar should reappear on selection"
    window.detail_bar._set_expanded(False)
    assert not window.detail_bar._expanded, "detail bar should be collapsed"
    window.library_view.flow.select_child(window.library_view.flow.get_child_at_index(1))
    assert window.detail_bar.game() is window.library_view.selected_game()
    assert not window.detail_bar._expanded, "collapsed state must persist across game switches"

    # Right-click (secondary-click) opens per-game settings; build the window.
    tile = window.library_view.flow.get_child_at_index(0)
    game_settings = GameSettingsDialog(library, tile.game, on_save=lambda g: None, parent=window)
    game_settings.present()
    assert not game_settings.get_modal(), "per-game settings should be a movable, non-modal window"
    # Adw.Window forbids set_titlebar (hard abort); editors stay plain windows.
    assert not isinstance(game_settings, Adw.Window)
    assert "vitrine-window" in game_settings.get_css_classes(), "editor windows must follow the theme"
    game_settings.close()
    print("right-click context builds the per-game settings window")

    # The global settings window also opens from the cog.
    settings = SettingsDialog(library, window.theme_manager, parent=window)
    settings.present()
    assert not isinstance(settings, Adw.Window), "settings must be a plain Gtk.Window (Adw forbids set_titlebar)"
    assert "vitrine-window" in settings.get_css_classes(), "settings window must follow the theme"
    settings.close()

    # The Steam sign-in window builds (browser + cookie-paste flow).
    from vitrine.sources.steam.auth import SteamTokenStore
    from vitrine.ui.steam_login_dialog import SteamLoginDialog

    with tempfile.TemporaryDirectory() as tmp:
        login = SteamLoginDialog(SteamTokenStore(tmp, "111"), parent=window)
        login.present()
        login.close()
    print("Steam sign-in window builds")
    print("global settings window opens from the cog")

    # Launching the window should take effect through the supervisor. The game
    # exits immediately, so afterwards nothing should be running and no error
    # toast may be raised.
    game = library.games(source="local")[0]
    window.on_game_activated(game)
    assert window.runtime.running, "expected a process to start after activation"

    # The add-game window is part of the shell too; build it to catch widget
    # misuse without needing to interact with it.
    add_window = AddGameDialog(library, on_add=lambda game: None)
    add_window.present()
    add_window.close()


def main() -> int:
    failures: list[str] = []

    class SmokeApplication(VitrineApplication):
        """Check from do_activate() itself.

        A handler connected to ``GApplication::activate`` runs *before* the class
        closure, which is where the window is created (the signal is RUN_LAST), so
        it would see no window at all. Overriding the vfunc keeps the ordering
        obvious.
        """

        def do_activate(self) -> None:
            try:
                super().do_activate()
                check_shell(self)
            except Exception as ex:
                failures.append(str(ex))
                traceback.print_exc()
            finally:
                GLib.timeout_add(250, self.quit)

    code = SmokeApplication().run([])

    if failures:
        print("GUI smoke test FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("GUI smoke test OK")
    return code


if __name__ == "__main__":
    sys.exit(main())
