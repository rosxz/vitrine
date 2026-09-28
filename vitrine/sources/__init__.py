"""Source package: importing it registers every available source."""

from vitrine.sources import (
    epic_source,  # noqa: F401  (imported for its registration side effect)
    gog_source,  # noqa: F401  (imported for its registration side effect)
    local,  # noqa: F401  (imported for its registration side effect)
    steam_source,  # noqa: F401  (imported for its registration side effect)
)
from vitrine.sources.base import Source, SourceGame, registry
from vitrine.sources.epic.auth import EpicAuthError, EpicTokenStore
from vitrine.sources.gog.auth import GogAuthError, GogTokenStore
from vitrine.sources.steam.auth import CookieJar, SteamAuthError, SteamTokenStore

__all__ = [
    "Source",
    "SourceGame",
    "registry",
    "CookieJar",
    "SteamTokenStore",
    "SteamAuthError",
    "GogTokenStore",
    "GogAuthError",
    "EpicTokenStore",
    "EpicAuthError",
]
