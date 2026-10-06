"""The game library: CRUD repository + settings over the database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from vitrine.domain.game import DEFAULT_CONFIG, Game
from vitrine.infra.util import now, slugify

if TYPE_CHECKING:
    from vitrine.sources.base import SourceGame

#: Setting key (boolean): open the live execution log window automatically for
#: installs/launches across every source.
DEBUG_LOG_SETTING = "auto_show_debug_log"

#: Setting key (boolean): reveal hidden/blacklisted games in the lists.
SHOW_HIDDEN = "show_hidden"

#: Setting key (boolean): show the game description / hero detail bar on selection.
SHOW_DETAIL_SETTING = "show_detail_bar"

#: Setting key (boolean): hide the window while a game runs and restore it on exit.
MINIMIZE_TO_TRAY_SETTING = "minimize_to_tray"

#: Setting key (boolean): draw the selected game's blurred artwork behind the
#: library grid. Independent of :data:`SHOW_DETAIL_SETTING`.
BACKGROUND_IMAGE_SETTING = "background_image"
#: Setting key (int): backdrop blur radius in logical pixels (0-100).
BACKGROUND_BLUR_SETTING = "background_blur"
#: Default backdrop blur radius (px).
DEFAULT_BACKGROUND_BLUR = 40
#: Largest allowed backdrop blur radius (px).
MAX_BACKGROUND_BLUR = 100

#: Setting key (int): width of a game tile in the grid, in logical pixels.
TILE_SIZE_SETTING = "tile_size"
#: Default / minimum / maximum game-tile width (px). Must stay in sync with the
#: aspect ratio used by :mod:`vitrine.ui.library_view`.
DEFAULT_TILE_SIZE = 180
MIN_TILE_SIZE = 120
MAX_TILE_SIZE = 280

#: Setting key (boolean): write the exact Proton launch command + environment to
#: ``$XDG_CACHE_HOME/vitrine/proton-launch.env`` on every launch, for debugging
#: window-presentation issues. Off by default -- it's a diagnostics aid only.
DUMP_LAUNCH_ENV_SETTING = "dump_launch_env"


class Library:
    """CRUD for games, plus settings access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def migrate_artwork_source_default(self) -> int:
        """Migrate the legacy ``lutris`` artwork default to ``auto``.

        Before the artwork providers, every store game defaulted to the ``lutris``
        source. Now that ``auto`` is the default (and its priority chain includes
        Lutris as a fallback), convert those implicit-default rows so existing
        games gain IGDB/SteamGridDB coverage. Users who explicitly pick a source
        later can still do so — and ``auto`` keeps Lutris art reachable anyway.
        Returns how many rows were changed.
        """
        cursor = self.conn.execute(
            "UPDATE games SET artwork_source = 'auto' WHERE artwork_source = 'lutris'"
        )
        self.conn.commit()
        return max(cursor.rowcount, 0)

    # -- games -----------------------------------------------------------------

    def games(self, source: str | None = None, favorite: bool | None = None) -> list[Game]:
        query = "SELECT * FROM games WHERE 1 = 1"
        params: list[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if favorite is not None:
            query += " AND favorite = ?"
            params.append(int(bool(favorite)))
        query += " ORDER BY COALESCE(sortname, name) COLLATE NOCASE"
        return [Game.from_row(row) for row in self.conn.execute(query, params)]

    def favorite_games(self) -> list[Game]:
        return self.games(favorite=True)

    def set_favorite(self, game_id: int | None, favorite: bool) -> None:
        """Mark a game as starred/favorite. No-op without a row id."""
        if game_id is None:
            return
        self.conn.execute(
            "UPDATE games SET favorite = ?, updated_at = ? WHERE id = ?",
            (int(bool(favorite)), now(), game_id),
        )
        self.conn.commit()

    def set_hidden(self, game_id: int | None, hidden: bool, source_id: str | None = None) -> None:
        """Hide/blacklist a game. Uses the row id when available, else source_id.

        Store-synced games may not have been persisted with an id yet; falling
        back to ``(source, source_id)`` keeps hiding robust for those rows.
        """
        if game_id is not None:
            self.conn.execute(
                "UPDATE games SET hidden = ?, updated_at = ? WHERE id = ?",
                (int(bool(hidden)), now(), game_id),
            )
        elif source_id:
            self.conn.execute(
                "UPDATE games SET hidden = ?, updated_at = ? WHERE source_id = ?",
                (int(bool(hidden)), now(), source_id),
            )
        else:
            return
        self.conn.commit()

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

    def set_authoritative_playtime(self, game: Game, hours: float, lastplayed: int | None = None) -> None:
        """Overwrite a game's playtime with an authoritative source value.

        Used for stores (e.g. Steam) that own their playtime on the server, so we
        mirror their number rather than accumulating our own wall-clock time.
        """
        fresh = self.game(game.id) if game.id is not None else None
        target = fresh or game
        target.playtime = float(hours)
        if lastplayed is not None:
            target.lastplayed = lastplayed
        self.update(target)
        return target

    # -- achievements ----------------------------------------------------------

    def achievements_for(self, game: Game, provider: str | None = None) -> list:
        """Return a game's stored achievements as ``Achievement`` objects."""
        from vitrine.domain.achievement import Achievement

        if game.id is None:
            return []
        query = "SELECT * FROM achievements WHERE game_id = ?"
        params: list[Any] = [game.id]
        if provider:
            query += " AND provider = ?"
            params.append(provider)
        query += " ORDER BY sort, key COLLATE NOCASE"
        rows = self.conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            out.append(
                Achievement(
                    key=row["key"],
                    name=row["name"] or "",
                    description=row["description"] or "",
                    hidden=bool(row["hidden"]),
                    unlocked=bool(row["unlocked"]),
                    unlock_date=row["unlock_date"],
                    progress=float(row["progress"] or 0),
                    xp=row["xp"],
                    tier=row["tier"],
                    rarity=row["rarity"],
                    icon_locked_path=row["icon_locked"],
                    icon_unlocked_path=row["icon_unlocked"],
                    sort=int(row["sort"] or 0),
                )
            )
        return out

    def replace_achievements(self, game: Game, achievement_set) -> None:
        """Replace a game's stored achievements with an :class:`AchievementSet`.

        The achiever's icons are already cached by the caller; this persists rows
        (removing ones no longer reported) and refreshes the cached summary on the
        ``games`` row. Returns nothing; the game object is updated in place if it
        has an id.
        """
        if game.id is None:
            return
        now_ts = now()
        self.conn.execute("DELETE FROM achievements WHERE game_id = ?", (game.id,))
        rows = []
        for ach in achievement_set.achievements:
            rows.append(
                (
                    game.id,
                    achievement_set.provider,
                    ach.key,
                    ach.name,
                    ach.description,
                    int(ach.hidden),
                    int(ach.unlocked),
                    ach.unlock_date,
                    float(ach.progress),
                    ach.xp,
                    ach.tier,
                    ach.rarity,
                    ach.icon_locked_path,
                    ach.icon_unlocked_path,
                    int(ach.sort),
                    now_ts,
                )
            )
        self.conn.executemany(
            "INSERT INTO achievements (game_id, provider, key, name, description, hidden,"
            " unlocked, unlock_date, progress, xp, tier, rarity, icon_locked, icon_unlocked,"
            " sort, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        game.achievement_count = achievement_set.total
        game.achievement_unlocked = achievement_set.unlocked
        self.update(game)
        self.conn.commit()

    def clear_achievements(self, game: Game) -> None:
        """Remove all stored achievements for a game and reset its summary."""
        if game.id is None:
            return
        self.conn.execute("DELETE FROM achievements WHERE game_id = ?", (game.id,))
        game.achievement_count = None
        game.achievement_unlocked = None
        self.update(game)
        self.conn.commit()

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

    def merge_source_games(self, source: str, games: Iterable[SourceGame]) -> list[Game]:
        """Upsert catalogue games into the library as installable entries.

        Owned-but-not-installed entries appear in the grid too; they carry a
        ``source``/``source_id`` so the UI can link to the store. Returns the
        newly-inserted games (best-effort) so callers can fetch artwork only
        for brand-new entries.
        """
        new_games: list[Game] = []
        for catalog in games:
            if not all(hasattr(catalog, attr) for attr in ("appid", "name", "slug", "installed")):
                continue
            details = getattr(catalog, "details", None) or {}
            hours = details.get("playtime_hours")
            lastplayed = details.get("lastplayed")
            existing = self.game_by_source_id(source, catalog.appid)
            if existing is None:
                game = Game(
                    name=catalog.name,
                    slug=catalog.slug or slugify(catalog.name),
                    runner=getattr(catalog, "runner", "wine") or "wine",
                    source=source,
                    source_id=catalog.appid,
                    installed=catalog.installed,
                    playtime=float(hours) if hours is not None else 0.0,
                    lastplayed=int(lastplayed) if lastplayed is not None else None,
                )
                self.add(game)
                new_games.append(game)
            else:
                dirty = False
                if existing.name != catalog.name:
                    existing.name = catalog.name
                    dirty = True
                if existing.installed != catalog.installed:
                    existing.installed = catalog.installed
                    dirty = True
                # Steam's authoritative playtime: overwrite (never accumulate) so
                # Vitrine mirrors Steam. Only sources that emit ``playtime_hours``
                # participate; GOG/Epic entries carry no such key and are ignored.
                if hours is not None and float(hours) != existing.playtime:
                    existing.playtime = float(hours)
                    dirty = True
                if lastplayed is not None and int(lastplayed) != existing.lastplayed:
                    existing.lastplayed = int(lastplayed)
                    dirty = True
                if dirty:
                    self.update(existing)
        return new_games

    def remove_source_game(self, source: str, source_id: str) -> None:
        """Remove a library entry that came from a store catalogue."""
        game = self.game_by_source_id(source, source_id)
        if game is not None and game.id is not None:
            self.remove(game.id)
        self.conn.execute(
            "DELETE FROM source_games WHERE source = ? AND appid = ?", (source, source_id)
        )
        self.conn.commit()

    def prune_source_games(
        self,
        source: str,
        keep_appids: Iterable[str] | None = None,
        keep_installed: bool = True,
        preserve_on_disk: Iterable[str] | None = None,
    ) -> int:
        """Delete library entries for a store that are no longer relevant.

        By default rows flagged ``installed`` are preserved even if the store
        no longer lists them. Pass ``keep_installed=False`` to drop every row
        not named by ``keep_appids``/``preserve_on_disk`` — used on a session
        reset, when the DB's ``installed`` flag can be stale (a played-but-
        uninstalled game wrongly marked installed). ``preserve_on_disk``
        carries the source's *authoritative* set of apps actually on disk.
        Returns how many rows were pruned.
        """
        keep = set(keep_appids or ())
        on_disk = set(preserve_on_disk or ())
        removed = 0
        for game in self.games(source=source):
            appid = game.source_id or ""
            if keep_installed and (game.installed or appid in on_disk):
                continue
            if appid in keep or appid in on_disk:
                continue
            if game.id is not None:
                self.remove(game.id)
            removed += 1
        return removed

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
        stored = self.setting("global_config", {}) or {}
        config = {**DEFAULT_CONFIG, **stored}
        if "runner" not in stored:
            config["runner"] = self.setting("default_runner", config["runner"])
        return config
