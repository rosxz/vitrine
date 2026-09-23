"""SQLite storage: connection handling, schema and migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import SCHEMA_VERSION, paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    sortname    TEXT,
    slug        TEXT NOT NULL UNIQUE,
    runner      TEXT NOT NULL DEFAULT 'wine',
    platform    TEXT,
    executable  TEXT,
    arguments   TEXT,
    working_dir TEXT,
    prefix      TEXT,
    source      TEXT NOT NULL DEFAULT 'local',
    source_id   TEXT,
    catalog_slug TEXT,
    year        INTEGER,
    installed   INTEGER NOT NULL DEFAULT 0,
    playtime    REAL NOT NULL DEFAULT 0,
    lastplayed  INTEGER,
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS games_source_idx ON games (source, source_id);

CREATE TABLE IF NOT EXISTS source_games (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    appid       TEXT NOT NULL,
    name        TEXT NOT NULL,
    slug        TEXT,
    catalog_slug TEXT,
    details     TEXT,
    updated_at  INTEGER,
    UNIQUE (source, appid)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

def _noop(conn: sqlite3.Connection) -> None:
    """No-op migration placeholder for untouched schema bumps."""
    return None


def _add_artwork_columns(conn: sqlite3.Connection) -> None:
    """v1 -> v2: optional per-game artwork paths (cover portrait, wide banner)."""
    conn.execute("ALTER TABLE games ADD COLUMN cover TEXT")
    conn.execute("ALTER TABLE games ADD COLUMN banner TEXT")


def _add_artwork_source(conn: sqlite3.Connection) -> None:
    """v2 -> v3: how each game gets its artwork plus an optional Lutris override.

    ``artwork_source`` is one of ``local`` (manual files picked by the user),
    ``lutris`` (fetched from the lutris.net database) or ``provider`` (art from
    the game's store). ``lutris_slug`` lets a user pin a specific Lutris entry
    instead of relying on the automated name match.
    """
    conn.execute("ALTER TABLE games ADD COLUMN artwork_source TEXT NOT NULL DEFAULT 'lutris'")
    conn.execute("ALTER TABLE games ADD COLUMN lutris_slug TEXT")


def _add_favorite_hidden(conn: sqlite3.Connection) -> None:
    """v3 -> v4: per-game flags for starring (favorites) and hiding (blacklist)."""
    conn.execute("ALTER TABLE games ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE games ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")


# Each future schema change gets a function here; ``initialize`` runs the ones
# this database has not seen yet. Index ``n`` upgrades version ``n`` to
# ``n + 1``.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _noop,  # v0 -> v1 (the initial walking skeleton created a fresh schema)
    _add_artwork_columns,  # v1 -> v2
    _add_artwork_source,  # v2 -> v3
    _add_favorite_hidden,  # v3 -> v4
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the library database. Pass ``":memory:"`` for tests."""
    if path is None:
        db_path: Path | str = paths.db_path()
    else:
        db_path = path

    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema. Safe to call on every start."""
    conn.executescript(SCHEMA)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(MIGRATIONS)):
        MIGRATIONS[index](conn)
        version = index + 1

    conn.execute(f"PRAGMA user_version = {max(version, SCHEMA_VERSION)}")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager committing on success and rolling back on error."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
