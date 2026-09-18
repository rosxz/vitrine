"""The game library: the domain model on top of the database."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from .util import now, slugify

DEFAULT_CONFIG: dict[str, Any] = {
    "graphics": "x11",  # "x11" or "wayland"
    "gamemode": False,
    "mangohud": False,
    "gamescope": False,
    "gamescope_window_mode": "-f",
    "gamescope_output_res": "",
    "gamescope_fps_limiter": "",
    "gamescope_flags": "",
    "gamescope_fsr_sharpness": "",
    "gamescope_force_grab_cursor": True,
    "gamescope_hdr": False,
    "wine_binary": None,
    "dll_overrides": "",
    "dxvk": True,
    "vkd3d": True,
    "esync": True,
    "fsync": True,
    "env": {},
    "pre_launch": [],
    "post_launch": [],
}


@dataclass
class Game:
    """A game entry. ``source`` names the provider it came from ('local', 'steam', ...)."""

    name: str
    id: int | None = None
    sortname: str | None = None
    slug: str = ""
    runner: str = "wine"
    platform: str | None = None
    executable: str | None = None
    arguments: str | None = None
    working_dir: str | None = None
    prefix: str | None = None
    source: str = "local"
    source_id: str | None = None
    catalog_slug: str | None = None
    year: int | None = None
    installed: bool = False
    playtime: float = 0.0
    lastplayed: int | None = None
    cover: str | None = None
    banner: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def merged_config(self, global_config: dict[str, Any]) -> dict[str, Any]:
        """Per-game values layered over the global defaults."""
        merged = {**DEFAULT_CONFIG, **global_config}
        for key, value in self.config.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            merged[key] = value
        return merged

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Game:
        data = dict(row)
        data["config"] = json.loads(data.get("config") or "{}")
        data["installed"] = bool(data.get("installed"))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("id", None)
        row["config"] = json.dumps(row["config"])
        row["installed"] = int(self.installed)
        return row


class Library:
    """CRUD for games, plus settings access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- games -----------------------------------------------------------------

    def games(self, source: str | None = None) -> list[Game]:
        query = "SELECT * FROM games"
        params: list[Any] = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY COALESCE(sortname, name) COLLATE NOCASE"
        return [Game.from_row(row) for row in self.conn.execute(query, params)]

    def game(self, game_id: int) -> Game | None:
        row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return Game.from_row(row) if row else None

    def game_by_source_id(self, source: str, source_id: str) -> Game | None:
        row = self.conn.execute(
            "SELECT * FROM games WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()
        return Game.from_row(row) if row else None

    def add(self, game: Game) -> Game:
        """Insert a game, assigning a unique slug if needed."""
        game.slug = game.slug or slugify(game.name)
        game.slug = self._unique_slug(game.slug)
        timestamp = now()
        row = game.to_row()
        row["created_at"] = timestamp
        row["updated_at"] = timestamp
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        cursor = self.conn.execute(f"INSERT INTO games ({columns}) VALUES ({placeholders})", list(row.values()))
        self.conn.commit()
        game.id = int(cursor.lastrowid or 0)
        return game

    def update(self, game: Game) -> None:
        if game.id is None:
            raise ValueError("Cannot update a game without an id")
        row = game.to_row()
        row["updated_at"] = now()
        assignments = ", ".join(f"{column} = ?" for column in row)
        self.conn.execute(f"UPDATE games SET {assignments} WHERE id = ?", [*row.values(), game.id])
        self.conn.commit()

    def remove(self, game_id: int) -> None:
        self.conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
        self.conn.commit()

    def record_playtime(self, game: Game, hours: float) -> None:
        """Accumulate playtime and stamp the last-played time."""
        game.playtime = float(game.playtime or 0) + hours
        game.lastplayed = now()
        self.update(game)

    def _unique_slug(self, base: str) -> str:
        candidate = base or "game"
        suffix = 2
        while self.conn.execute("SELECT 1 FROM games WHERE slug = ?", (candidate,)).fetchone():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # -- source games ----------------------------------------------------------

    def source_games(self, source: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM source_games WHERE source = ?", (source,)))

    def upsert_source_game(self, source: str, appid: str, name: str, **details: Any) -> None:
        """Insert or refresh a cached row from a store's catalogue."""
        self.conn.execute(
            "INSERT INTO source_games (source, appid, name, details, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, appid) DO UPDATE SET name = excluded.name, "
            "details = excluded.details, updated_at = excluded.updated_at",
            (source, appid, name, json.dumps(details), now()),
        )
        self.conn.commit()

    def clear_source_games(self, source: str) -> None:
        """Drop all cached catalogue rows for a store."""
        self.conn.execute("DELETE FROM source_games WHERE source = ?", (source,))
        self.conn.commit()

    # -- settings --------------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def global_config(self) -> dict[str, Any]:
        return {**DEFAULT_CONFIG, **(self.setting("global_config", {}) or {})}
