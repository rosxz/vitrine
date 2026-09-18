"""Schema migration tests: upgrading must add columns without losing rows."""

from __future__ import annotations

import sqlite3

from vitrine import db
from vitrine.library import Game, Library


def _connection_with_v1_schema() -> sqlite3.Connection:
    """Build a database whose schema is version 1 (pre-artwork) and seeded."""
    conn = db.connect(":memory:")
    # Run the current initialize, then drop the artwork columns to simulate v1.
    db.initialize(conn)
    conn.execute("ALTER TABLE games DROP COLUMN cover")
    conn.execute("ALTER TABLE games DROP COLUMN banner")
    conn.execute("PRAGMA user_version = 1")

    # Seed with plain SQL (the updated Game model carries the new columns).
    conn.execute(
        "INSERT INTO games (name, slug, source, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("Keep Me", "keep-me", "local", 1, 1),
    )
    conn.commit()
    return conn


def test_migration_v1_to_v2_adds_artwork_columns_and_keeps_rows() -> None:
    conn = _connection_with_v1_schema()
    before = Library(conn).games(source="local")
    assert len(before) == 1
    assert before[0].name == "Keep Me"

    # Re-initialising runs the pending migration (v1 -> v2).
    db.initialize(conn)

    columns = [row[1] for row in conn.execute("PRAGMA table_info(games)")]
    assert "cover" in columns
    assert "banner" in columns

    connected = Library(conn)
    after = connected.games(source="local")
    assert len(after) == 1
    assert after[0].name == "Keep Me"
    assert after[0].cover is None
    assert after[0].banner is None


def test_artwork_fields_round_trip() -> None:
    conn = db.connect(":memory:")
    db.initialize(conn)
    library = Library(conn)
    library.add(
        Game(
            name="With Art",
            slug="with-art",
            source="local",
            cover="/cover.png",
            banner="/banner.png",
        )
    )
    game = library.games(source="local")[0]
    assert game.cover == "/cover.png"
    assert game.banner == "/banner.png"