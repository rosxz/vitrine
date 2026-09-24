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


def test_tile_loads_cover_and_falls_back_when_absent() -> None:
    """Tiles must call set_cover(game.cover): a regression previously left the
    grid showing initials even after artwork was downloaded and persisted."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct tiles")
    import os
    import tempfile

    from gi.repository import GdkPixbuf

    from vitrine.library import Game
    from vitrine.ui.library_view import GameTile

    cover = os.path.join(tempfile.mkdtemp(), "cover.png")
    GdkPixbuf.Pixbuf.new(
        colorspace=GdkPixbuf.Colorspace.RGB,
        has_alpha=False,
        bits_per_sample=8,
        width=16,
        height=24,
    ).savev(cover, "png", [], [])

    with_cover = Game(name="W", source="steam", source_id="1", cover=cover)
    tile = GameTile(with_cover)
    # Lazy loading: the constructor shows the placeholder; calling load_cover
    # swaps in the artwork.
    assert tile.cover.get_paintable() is None
    tile.load_cover()
    # set_filename() resolves its paintable asynchronously; pump the loop.
    _pump_main_loop(50)
    assert tile.cover.get_paintable() is not None, "tile must render game.cover"

    no_cover = Game(name="N", source="local")
    blank = GameTile(no_cover)
    _pump_main_loop(5)
    # A blank tile paints nothing and shows the initials placeholder.
    assert blank.cover.get_paintable() is None


def test_store_not_installed_tiles_keep_translucent_class() -> None:
    """Owned-but-not-installed Steam/GOG/Epic tiles stay greyed (translucent),
    even after a selection change resets their css classes."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct tiles")
    from vitrine.library import Game
    from vitrine.ui.library_view import GameTile

    for source in ("steam", "gog", "epic"):
        tile = GameTile(Game(name="Owned", source=source, source_id="1", installed=False))
        assert "not-installed" in tile.get_css_classes(), f"{source}: owns tile must be translucent"

    installed = GameTile(Game(name="Installed", source="steam", source_id="2", installed=True))
    assert "not-installed" not in installed.get_css_classes()


def test_matrix_of_tiles_labels_game_tile_coverage() -> None:
    """Check each GameTile is created without loading its cover eagerly."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct tiles")
    from vitrine.library import Game
    from vitrine.ui.library_view import GameTile

    for source in ("local", "steam"):
        game = Game(name="M", source=source, source_id="1", cover="/tmp/missing.jpg")
        tile = GameTile(game)
        # Eager construction must not touch the (fake, non-existent) image.
        assert tile.cover.get_paintable() is None
        # Once demanded, load_cover is idempotent (no error on missing file).
        tile.load_cover()
        tile.load_cover()


def _pump_main_loop(iterations: int) -> None:
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(iterations):
        while context.pending():
            context.iteration(False)


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


def test_form_default_and_provider_visibility() -> None:
    """The default art source is auto, and local games hide the provider option."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct widgets")
    from vitrine.ui.game_form import GameForm

    local = GameForm(allow_provider=False)
    assert local.artwork_source() == "auto"
    assert "provider" not in local._source_buttons

    store = GameForm(allow_provider=True)
    assert store.artwork_source() == "auto"
    assert "provider" in store._source_buttons
    assert "lutris" in store._source_buttons
    assert "local" in store._source_buttons


def test_form_source_change_notifies_only_on_active() -> None:
    """Switching the art source fires the change callback for the new selection."""
    if not Gtk.init_check():
        pytest.skip("requires a display to construct widgets")
    from vitrine.ui.game_form import GameForm

    form = GameForm(allow_provider=True)
    seen: list[str] = []
    form.connect_source_changed(seen.append)

    form._source_buttons["provider"].set_active(True)
    assert seen and seen[-1] == "provider"

    form.set_artwork_source("lutris")
    # Programmatic population must not fire the callback.
    assert seen[-1] == "provider"


def test_gamescope_wrap_wraps_command() -> None:
    from vitrine.ui.window import _gamescope_wrap

    wrapped = _gamescope_wrap({}, ["legendary", "launch", "x"])
    assert wrapped[0] == "gamescope"
    assert wrapped[-1] == "x"
    assert wrapped[-2] == "launch"
    assert "--" in wrapped

    sized = _gamescope_wrap({"gamescope_output_res": "1920x1080"}, ["g"])
    assert sized[1:4] == ["-W", "1920", "-H"]
