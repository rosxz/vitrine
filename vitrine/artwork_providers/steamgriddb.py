"""SteamGridDB provider (API v2).

Community-submitted artwork: **heroes** (wide banners) are the strongest asset
here, so this provider is the default first choice for the hero slot. Portrait
grids cover the tile slot too.

Auth is a single API key passed as ``Authorization: Bearer <key>``. Games are
identified by Steam appid (fast, authoritative for Steam titles) or by a name
search falling back to the first autocomplete hit.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .base import SLOT_HERO, SLOT_TILE, Art
from .net import get_json

logger = logging.getLogger(__name__)

API = "https://www.steamgriddb.com/api/v2"

#: Portrait grid dimensions for the tile slot (height > width).
TILE_DIMENSIONS = "600x900"
#: Hero dimensions are wide; SteamGridDB serves them natively wide.
HERO_DIMENSIONS = "1920x620"


def configured(key: str | None) -> bool:
    return bool(key)


def probe(key: str) -> bool:
    """Best-effort reachability check: an authenticated autocomplete call returns
    JSON (even with zero hits) when the key is valid and reachable."""
    payload = get_json(f"{API}/search/autocomplete/test", headers=_auth(key))
    return payload is not None


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _game_id(key: str, game) -> int | None:
    """Resolve the SteamGridDB numeric game id for a game."""
    if getattr(game, "source", "") == "steam" and game.source_id:
        payload = get_json(
            f"{API}/game/steam/{quote(game.source_id)}", headers=_auth(key)
        )
        data = (payload or {}).get("success") and payload.get("data") or None
        if data and data.get("id"):
            return int(data["id"])
    term = getattr(game, "name", "") or ""
    if not term:
        return None
    payload = get_json(
        f"{API}/search/autocomplete/{quote(term)}", headers=_auth(key)
    )
    data = (payload or {}).get("success") and payload.get("data") or []
    if data and data[0].get("id") is not None:
        return int(data[0]["id"])
    return None


def tiles(key: str, game) -> list[Art]:
    gid = _game_id(key, game)
    if gid is None:
        return []
    payload = get_json(f"{API}/grids/game/{gid}?types=static&dimensions={quote(TILE_DIMENSIONS)}", headers=_auth(key))
    return [_art(d, SLOT_TILE) for d in _items(payload)]


def heroes(key: str, game) -> list[Art]:
    gid = _game_id(key, game)
    if gid is None:
        return []
    payload = get_json(f"{API}/heroes/game/{gid}", headers=_auth(key))
    return [_art(d, SLOT_HERO) for d in _items(payload)]


def _items(payload) -> list[dict]:
    if not payload:
        return []
    if not payload.get("success"):
        return []
    data = payload.get("data") or []
    return [d for d in data if isinstance(d, dict) and d.get("url")]


def _art(d: dict, slot: str) -> Art:
    style = d.get("style") or d.get("mime") or ""
    w, h = d.get("width") or 0, d.get("height") or 0
    dims = f"{w}x{h}" if w and h else ""
    label = "fill" if style == "material" else style
    return Art(
        provider="steamgriddb",
        slot=slot,
        url=d["url"],
        thumb=d.get("thumb") or d["url"],
        label=" ".join(filter(None, (label, dims))),
        width=w,
        height=h,
    )