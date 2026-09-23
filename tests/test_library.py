"""Library storage and settings."""

from __future__ import annotations

import pytest

from vitrine import SCHEMA_VERSION, db
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

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


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


def test_favorite_and_hidden_round_trip(library: Library) -> None:
    game = library.add(Game(name="Starred"))

    library.set_favorite(game.id, True)
    library.set_hidden(game.id, True)

    reloaded = library.game(game.id)
    assert reloaded.favorite is True
    assert reloaded.hidden is True

    assert [g.name for g in library.favorite_games()] == ["Starred"]
    assert [g.name for g in library.games(favorite=True)] == ["Starred"]
    assert [g.name for g in library.games(favorite=False)] == []

    library.set_favorite(game.id, False)
    library.set_hidden(game.id, False)
    reloaded = library.game(game.id)
    assert reloaded.favorite is False
    assert reloaded.hidden is False
    assert library.favorite_games() == []


def test_new_games_default_to_not_favorite_and_not_hidden() -> None:
    game = Game(name="Plain")
    assert game.favorite is False
    assert game.hidden is False


def test_set_hidden_falls_back_to_source_id(library: Library) -> None:
    library.add(Game(name="Store game", source="steam", source_id="99"))
    library.set_hidden(None, True, source_id="99")
    game = library.game_by_source_id("steam", "99")
    assert game is not None and game.hidden is True
