"""Steam CDN provider.

Candidates straight from Steam's image CDN keyed by appid. Cheap and no auth,
so it is a useful tile/hero fallback for Steam titles. Returns candidate lists
(not single URLs) so the downloader can try each in turn.
"""

from __future__ import annotations

from .base import Art

STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps/%s/%s"
STEAM_CDN_SHARED = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%s/%s"


def configured() -> bool:
    return True  # no key required


def tiles(appid: str) -> list[Art]:
    arts: list[Art] = []
    for kind, url in (
        ("library_600x900_2x.jpg", STEAM_CDN),
        ("library_600x900_2x.jpg", STEAM_CDN_SHARED),
        ("library_600x900.jpg", STEAM_CDN),
        ("header.jpg", STEAM_CDN),
        ("capsule_616x353.jpg", STEAM_CDN),
    ):
        arts.append(Art("steam", "tile", url % (appid, kind), thumb=url % (appid, kind), label=kind))
    return arts


def heroes(appid: str) -> list[Art]:
    arts: list[Art] = []
    for kind, url in (
        ("capsule_616x353.jpg", STEAM_CDN),
        ("capsule_616x353.jpg", STEAM_CDN_SHARED),
        ("header.jpg", STEAM_CDN),
    ):
        arts.append(Art("steam", "hero", url % (appid, kind), thumb=url % (appid, kind), label=kind))
    return arts


def candidates(game) -> dict[str, list[Art]]:
    appid = getattr(game, "source_id", "") or ""
    if getattr(game, "source", "") != "steam" or not appid:
        return {"tile": [], "hero": []}
    return {"tile": tiles(appid), "hero": heroes(appid)}