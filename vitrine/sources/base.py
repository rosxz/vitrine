"""Source providers: where games come from.

A source knows how to enumerate the games a user owns on it. Steam, GOG and
Epic will each add one; ``local`` covers games added by hand. Sources never
touch the GUI, so they can be synced from a worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceGame:
    """A game as reported by its store, before it becomes a library entry."""

    source: str
    appid: str
    name: str
    slug: str | None = None
    catalog_slug: str | None = None
    installed: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class Source(ABC):
    """Base class for a game source."""

    id: str = ""
    name: str = ""
    icon: str | None = None
    #: True if the source needs an account before it can list anything.
    requires_auth: bool = False

    @abstractmethod
    def sync(self) -> int:
        """Refresh this source's games. Returns how many are now known."""

    def sync_installed(self) -> int:
        """Mark which of this source's games are installed locally.

        Sources that cannot tell return 0 and leave the flags alone.
        """
        return 0

    def is_configured(self) -> bool:
        """Whether the source has everything it needs to sync."""
        return True


class SourceRegistry:
    """The set of sources the app knows about."""

    def __init__(self) -> None:
        self._sources: dict[str, type[Source]] = {}

    def register(self, source: type[Source]) -> type[Source]:
        self._sources[source.id] = source
        return source

    def get(self, source_id: str) -> type[Source] | None:
        return self._sources.get(source_id)

    def all(self) -> list[type[Source]]:
        return list(self._sources.values())


registry = SourceRegistry()
