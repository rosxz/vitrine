"""Artwork providers: IGDB, SteamGridDB, Lutris, Steam CDN.

Each provider yields :class:`Art` candidates for a game's tiles (portrait) and
heroes (wide). Credentials are passed in as small typed dicts; providers that
require a key are skipped when their key is absent.
"""

from __future__ import annotations

import logging
from typing import Any

from . import igdb, lutris, steam, steamgriddb
from .base import Art, CandidateSet

logger = logging.getLogger(__name__)

PROVIDER_IDS = ("igdb", "steamgriddb", "lutris", "steam")


def credentials_for(provider: str, creds: dict[str, Any]) -> bool:
    """Whether ``provider`` is usable with the supplied ``creds``."""
    if provider == "igdb":
        return igdb.configured(creds.get("igdb_client_id"), creds.get("igdb_client_secret"))
    if provider == "steamgriddb":
        return steamgriddb.configured(creds.get("steamgriddb_key"))
    return True  # lutris / steam need no key


def configured_providers(creds: dict[str, Any]) -> list[str]:
    return [p for p in PROVIDER_IDS if credentials_for(p, creds)]


def candidates_for(game, creds: dict[str, Any]) -> CandidateSet:
    """Gather candidates for ``game`` from every configured provider."""
    result = CandidateSet(game_key=getattr(game, "name", "") or "")
    if credentials_for("igdb", creds):
        try:
            for art in _both(
                igdb.candidates(creds["igdb_client_id"], creds["igdb_client_secret"], game)
            ):
                result.add(art)
        except Exception:  # noqa: BLE001 - one provider failing must not block others
            logger.exception("IGDB artwork failed for %s", getattr(game, "name", ""))
    if credentials_for("steamgriddb", creds):
        try:
            for art in steamgriddb.tiles(creds["steamgriddb_key"], game):
                result.add(art)
            for art in steamgriddb.heroes(creds["steamgriddb_key"], game):
                result.add(art)
        except Exception:  # noqa: BLE001
            logger.exception("SteamGridDB artwork failed for %s", getattr(game, "name", ""))
    try:
        for art in _both(steam.candidates(game)):
            result.add(art)
    except Exception:  # noqa: BLE001
        logger.exception("Steam artwork failed for %s", getattr(game, "name", ""))
    try:
        for art in lutris.from_slug(getattr(game, "lutris_slug", "") or "") or _lutris_by_name(game):
            result.add(art)
    except Exception:  # noqa: BLE001
        logger.exception("Lutris artwork failed for %s", getattr(game, "name", ""))
    return result


def _lutris_by_name(game) -> list[Art]:
    if not getattr(game, "name", ""):
        return []
    return lutris.search(getattr(game, "name", ""))


def _both(buckets: dict[str, list[Art]]) -> list[Art]:
    return (buckets.get("tile") or []) + (buckets.get("hero") or [])