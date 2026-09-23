"""IGDB (Twitch) provider, API v4.

IGDB's strength is authoritative cover art (portrait, good tile slot) and it
also exposes official artworks/screenshots that crop into acceptable heroes. It
needs Twitch Dev credentials: a Client-ID + Client-Secret, exchanged for a
short-lived client-credentials access token which we cache and refresh.

Queries use IGDB's Apicalypse body syntax over POST /v4/games. We respect the
shared rate limiter (IGDB caps at ~4 req/s).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Art
from .net import post_json

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4"
# The images host already prefixes sizes with ``t_`` (e.g. t_cover_big_2x), so the
# size slugs below must NOT include the leading ``t_``.
IMAGES = "https://images.igdb.com/igdb/image/upload/t_{size}/{id}.jpg"

#: Smallest cover size we consider for the tile slot.
_COVER_SIZE = "cover_big_2x"
#: Hero source sizes (artworks/screenshots); ordered by preference.
_HERO_SIZES = ("1080p", "720p", "original")
#: Preview thumb size used by the picker.
_THUMB = "thumb"
#: Steam external-game category in IGDB's ``external_games``.
_STEAM_CATEGORY = 1

#: Cache of {client_id|secret: (token, expires_at_unix)}.
_token_cache: dict[str, tuple[str, float]] = {}


def configured(client_id: str | None, client_secret: str | None) -> bool:
    return bool(client_id and client_secret)


def probe(client_id: str, client_secret: str) -> bool:
    """Best-effort reachability check: a successful token exchange means the
    credentials are valid and the API is reachable."""
    return _token(client_id, client_secret) is not None


def _token(client_id: str, client_secret: str) -> str | None:
    import time

    cache_key = f"{client_id}|{client_secret}"
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    payload = post_json(
        TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    if not payload or not payload.get("access_token"):
        return None
    token = payload["access_token"]
    _token_cache[cache_key] = (token, time.time() + int(payload.get("expires_in", 3600)))
    return token


def _headers(client_id: str, token: str) -> dict[str, str]:
    return {"Client-ID": client_id, "Authorization": f"Bearer {token}"}


def _query(client_id: str, client_secret: str, body: str) -> list[dict[str, Any]]:
    token = _token(client_id, client_secret)
    if not token:
        return []
    payload = post_json(
        f"{API}/games",
        headers=_headers(client_id, token),
        data=body,
        timeout=20,
    )
    return payload if isinstance(payload, list) else []


def candidates(client_id: str, client_secret: str, game) -> dict[str, list[Art]]:
    """Return ``{tile: [...], hero: [...]}`` candidates for a game."""
    games = _query(client_id, client_secret, _search_body(game))
    # A Steam-appid match can miss (old/niche titles); fall back to name search so
    # IGDB still contributes where SteamGridDB/Steam don't.
    if not games and getattr(game, "source", "") == "steam":
        games = _query(client_id, client_secret, _name_body(game))
    if not games:
        return {"tile": [], "hero": []}
    match = games[0]
    return {
        "tile": _covers(match),
        "hero": _heroes(match),
    }


def _search_body(game) -> str:
    if getattr(game, "source", "") == "steam" and game.source_id:
        # Authoritative match by Steam external id.
        return (
            f'fields name, cover.image_id, artworks.image_id, screenshots.image_id; '
            f'where external_games.category = {_STEAM_CATEGORY} & external_games.uid = "{game.source_id}"; '
            f"limit 1;"
        )
    return _name_body(game)


def _name_body(game) -> str:
    term = (getattr(game, "name", "") or "").strip()
    safe = term.replace('"', '\\"')
    return (
        f'search "{safe}"; fields name, cover.image_id, artworks.image_id, screenshots.image_id; limit 1;'
    )


def _covers(match: dict) -> list[Art]:
    cover = match.get("cover") or {}
    image_id = cover.get("image_id")
    arts: list[Art] = []
    if image_id:
        arts.append(
            Art("igdb", "tile", IMAGES.format(size=_COVER_SIZE, id=image_id),
                thumb=IMAGES.format(size=_THUMB, id=image_id), label="Cover")
        )
    return arts


def _heroes(match: dict) -> list[Art]:
    arts: list[Art] = []
    for kind in ("artworks", "screenshots"):
        for entry in match.get(kind) or []:
            image_id = entry.get("image_id")
            if not image_id:
                continue
            size = _HERO_SIZES[0] if kind == "artworks" else _HERO_SIZES[1]
            arts.append(
                Art(
                    "igdb",
                    "hero",
                    IMAGES.format(size=size, id=image_id),
                    thumb=IMAGES.format(size=_THUMB, id=image_id),
                    label="artwork" if kind == "artworks" else "screenshot",
                )
            )
    return arts