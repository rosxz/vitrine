"""Lutris.net provider.

Wraps the lutris.net game database (search by name/slug) returning its canonical
portrait cover and wide banner as candidates. Kept as a fallback provider --
its API is not a *clearly* sanctioned public endpoint, so it only runs when a
game has no better source.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from .base import Art
from .net import get_json

logger = logging.getLogger(__name__)

API = "https://lutris.net/api/games"
USER_AGENT_HEADER = {"User-Agent": "Vitrine/0.1 (game library launcher; artwork metadata lookup)"}


def _auth() -> dict[str, str]:
    return USER_AGENT_HEADER


def configured() -> bool:
    return True  # no key required


def _game_data(slug: str) -> dict | None:
    return get_json(f"{API}/{slug}", headers=_auth())


def search(name: str) -> list[Art]:
    """Search lutris.net for a game, returning portrait+hero candidates."""
    payload = get_json(f"{API}?{urlencode({'search': name, 'page_size': 10})}", headers=_auth())
    results = payload.get("results", []) if isinstance(payload, dict) else []
    slug = next((g.get("slug") for g in results if g.get("slug")), None)
    if not slug:
        return []
    return from_slug(slug)


def from_slug(slug: str) -> list[Art]:
    game = _game_data(slug)
    if not isinstance(game, dict):
        return []
    arts: list[Art] = []
    cover = game.get("coverart")
    banner = game.get("banner_url")
    if cover:
        arts.append(Art("lutris", "tile", str(cover), thumb=str(cover), label="Cover"))
    if banner:
        arts.append(Art("lutris", "hero", str(banner), thumb=str(banner), label="Banner"))
    return arts