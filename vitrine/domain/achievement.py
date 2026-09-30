"""Achievement data model (GUI-free value objects).

An :class:`Achievement` describes one unlockable achievement for a game, and an
:class:`AchievementSet` groups a source's achievements with a summary. Providers
(Steam/GOG/Epic) parse their store payloads into these DTOs; the achievements
service persists them and the UI renders them, without any provider knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: ``achievements_source`` values on a ``Game``.
#: ``auto`` derives the provider from the game's store source; ``none`` disables.
ACHIEVEMENTS_SOURCE_AUTO = "auto"
ACHIEVEMENTS_SOURCE_NONE = "none"
ACHIEVEMENTS_SOURCES = (ACHIEVEMENTS_SOURCE_AUTO, ACHIEVEMENTS_SOURCE_NONE)


@dataclass(frozen=True)
class Achievement:
    """One achievement definition, merged with the user's progress."""

    #: Stable store key (e.g. Steam apiname, GOG achievement_key).
    key: str
    #: Human-facing title.
    name: str = ""
    description: str = ""
    #: Whether the achievement is hidden until unlocked.
    hidden: bool = False
    #: Whether this user has unlocked it.
    unlocked: bool = False
    #: Unix timestamp of the unlock, or None.
    unlock_date: int | None = None
    #: Normalised completion (0..1) for games with partial progress.
    progress: float = 0.0
    #: Optional achievement points/experience (Epic).
    xp: int | None = None
    #: Optional tier/gold/silver/bronze (Epic) or rarity.
    tier: str | None = None
    rarity: float | None = None
    #: Remote icon URLs (pre-cache).
    icon_locked_url: str | None = None
    icon_unlocked_url: str | None = None
    #: Local cached icon paths (populated by the caching layer).
    icon_locked_path: str | None = None
    icon_unlocked_path: str | None = None
    #: Display order.
    sort: int = 0


@dataclass(frozen=True)
class AchievementSet:
    """A source's achievements for a game, with a summary."""

    provider: str
    achievements: tuple[Achievement, ...] = ()
    total: int = 0
    unlocked: int = 0

    @classmethod
    def build(cls, provider: str, items: Iterable[Achievement]) -> AchievementSet:
        items = tuple(sorted(items, key=lambda a: (a.sort, a.key or "")))
        total = len(items)
        unlocked = sum(1 for a in items if a.unlocked)
        return cls(provider=provider, achievements=items, total=total, unlocked=unlocked)

    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(self.unlocked / self.total, 1.0)