"""Source package: importing it registers every available source."""

from . import (
    local,  # noqa: F401  (imported for its registration side effect)
    steam_source,  # noqa: F401  (imported for its registration side effect)
)
from .base import Source, SourceGame, registry
from .steam.auth import CookieJar, SteamAuthError, SteamTokenStore

__all__ = ["Source", "SourceGame", "registry", "CookieJar", "SteamTokenStore", "SteamAuthError"]
