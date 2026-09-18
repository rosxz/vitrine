"""Guard against GTK/libadwaita API drift.

These tests only import the GUI modules and check that the widgets the UI relies
on still exist, which catches typos and version regressions without needing a
display. The GTK import has to follow the version pins.
"""

# ruff: noqa: E402

from __future__ import annotations

import pytest

from vitrine.ui import library_view

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk


def test_gui_modules_import() -> None:
    import vitrine.application  # noqa: F401
    import vitrine.ui.game_detail_bar  # noqa: F401
    import vitrine.ui.game_dialogs  # noqa: F401
    import vitrine.ui.library_view  # noqa: F401
    import vitrine.ui.settings_dialog  # noqa: F401
    import vitrine.ui.window  # noqa: F401


@pytest.mark.parametrize(
    "widget",
    ["ToolbarView", "OverlaySplitView", "ToastOverlay", "StatusPage", "HeaderBar", "WindowTitle", "EntryRow"],
)
def test_libadwaita_widgets_are_available(widget: str) -> None:
    assert hasattr(Adw, widget), f"libadwaita is missing Adw.{widget}"


@pytest.mark.parametrize("widget", ["AspectFrame", "FlowBox", "FlowBoxChild", "Picture", "FileDialog", "ContentFit"])
def test_gtk_widgets_are_available(widget: str) -> None:
    assert hasattr(Gtk, widget), f"GTK is missing Gtk.{widget}"


def test_cover_tiles_are_portrait() -> None:
    assert library_view.COVER_RATIO < 1, "covers must be portrait (2:3)"


def test_save_button_invokes_callback_with_game() -> None:
    """Regression: the save button must fire the callback with the Game, not
    with the clicked Button (a prior name shadowing bug passed the widget)."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct windows")
    from vitrine import db
    from vitrine.library import Game, Library
    from vitrine.ui.game_dialogs import GameSettingsDialog

    conn = db.connect(":memory:")
    db.initialize(conn)
    library = Library(conn)
    game = library.add(Game(name="Edit Me", source="local"))

    received: list = []
    dialog = GameSettingsDialog(library, game, on_save=received.append)
    dialog._on_save(Gtk.Button(label="fake"))
    assert received, "expected the save callback to fire"
    assert isinstance(received[0], Game), f"callback received {type(received[0]).__name__}, expected Game"
    assert received[0].id == game.id
