"""Shared types for the artwork providers.

A provider yields :class:`Art` candidates: an image URL (full resolution for
downloading) plus an optional small preview URL for the picker, tagged with the
provider id and which slot (``tile`` portrait cover or ``hero`` wide banner) it
satisfies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SLOT_TILE = "tile"
SLOT_HERO = "hero"


@dataclass(frozen=True)
class Art:
    """A single artwork candidate (an image) from a provider."""

    provider: str            # id, e.g. "igdb", "steamgriddb", "lutris", "steam"
    slot: str                # SLOT_TILE or SLOT_HERO
    url: str                 # full-resolution image URL to download/cache
    thumb: str = ""          # small preview URL for the picker (optional)
    label: str = ""          # short human description (style/frame/widthxheight)
    width: int = 0
    height: int = 0

    def key(self) -> str:
        return f"{self.provider}|{self.slot}|{self.url}"


@dataclass
class CandidateSet:
    """All candidates gathered for a game, grouped by slot and provider."""

    game_key: str = ""
    by_provider: dict[str, dict[str, list[Art]]] = field(default_factory=dict)

    def add(self, art: Art) -> None:
        self.by_provider.setdefault(art.provider, {}).setdefault(art.slot, []).append(art)

    def tiles(self, *providers: str) -> list[Art]:
        return self._slot(SLOT_TILE, *providers)

    def heroes(self, *providers: str) -> list[Art]:
        return self._slot(SLOT_HERO, *providers)

    def _slot(self, slot: str, *providers: str) -> list[Art]:
        out: list[Art] = []
        ids = providers or list(self.by_provider)
        for provider in ids:
            out.extend(self.by_provider.get(provider, {}).get(slot, []))
        return out

    def all(self) -> list[Art]:
        out: list[Art] = []
        for _prov, slots in self.by_provider.items():
            for _slot, arts in slots.items():
                out.extend(arts)
        return out


def provider_label(provider: str) -> str:
    # The Steam CDN source is the game store's own artwork provider, so it's
    # labelled "Provider" (matching the per-game artwork source) rather than
    # singling out one store (it would apply to Steam, Epic and GOG alike).
    return {
        "igdb": "IGDB",
        "steamgriddb": "SteamGridDB",
        "lutris": "Lutris",
        "steam": "Provider",
    }.get(provider, provider.title())