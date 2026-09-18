"""Library storage and settings."""

from __future__ import annotations

import pytest

from vitrine import db
from vitrine.library import DEFAULT_CONFIG, Game, Library


@pytest.fixture
def library() -> Library:
    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def test_add_assigns_id_and_slug(library: Library) -> None:
    game = library.add(Game(name="Shadow of the Tomb Raider"))

    assert game.id is not None
    assert game.slug == "shadow-of-the-tomb-raider"
    assert library.game(game.id).name == "Shadow of the Tomb Raider"


def test_slugs_stay_unique(library: Library) -> None:
    first = library.add(Game(name="Celeste"))
    second = library.add(Game(name="Celeste"))

    assert first.slug == "celeste"
    assert second.slug == "celeste-2"


def test_initialize_is_idempotent() -> None:
    conn = db.connect(":memory:")
    db.initialize(conn)
    db.initialize(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_games_can_be_filtered_by_source(library: Library) -> None:
    library.add(Game(name="Manual game", source="local"))
    library.add(Game(name="Steam game", source="steam", source_id="440"))

    assert [game.name for game in library.games(source="local")] == ["Manual game"]
    assert library.game_by_source_id("steam", "440").name == "Steam game"


def test_settings_round_trip_json(library: Library) -> None:
    library.set_setting("window", {"width": 1280, "height": 720})

    assert library.setting("window") == {"width": 1280, "height": 720}
    assert library.setting("missing", "fallback") == "fallback"


def test_per_game_config_overrides_global(library: Library) -> None:
    game = Game(name="Test", config={"gamescope": True, "env": {"DXVK_HUD": "fps"}})

    merged = game.merged_config({"gamemode": True, "gamescope": False})

    assert merged["gamemode"] is True           # from global
    assert merged["gamescope"] is True          # per-game wins
    assert merged["env"] == {"DXVK_HUD": "fps"}  # non-empty per-game value
    assert merged["dll_overrides"] == DEFAULT_CONFIG["dll_overrides"]


def test_record_playtime_accumulates(library: Library) -> None:
    game = library.add(Game(name="Hades"))

    library.record_playtime(game, 1.5)

    assert game.playtime == pytest.approx(1.5)
    assert game.lastplayed is not None
    assert library.game(game.id).playtime == pytest.approx(1.5)


def test_remove_deletes_the_row(library: Library) -> None:
    game = library.add(Game(name="Doomed"))

    library.remove(game.id)

    assert library.game(game.id) is None
