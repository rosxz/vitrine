"""Achievement provider protocol and shared helpers.

An achievement provider turns a game + credential context into an
:class:`AchievementSet`. Providers are GUI-free and never touch the library's
sqlite connection -- credentials arrive as a plain dict snapshot (built on the
main thread) and workers return DTOs for the orchestration layer to persist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from vitrine.domain.achievement import AchievementSet


class AchievementProvider(Protocol):
    """Fetch a game's achievements from one store."""

    #: Provider id ('steam', 'gog', 'epic', ...).
    id: str

    def configured(self, ctx: dict[str, Any]) -> bool:
        """Whether this provider can run with the credentials in ``ctx``."""
        ...

    def fetch(self, game: Any, ctx: dict[str, Any]) -> AchievementSet | None:
        """Return achievements for ``game``, or ``None`` if none/unavailable."""
        ...


#: Registry of provider ids -> ``fetch(game, ctx)`` callables.
_PROVIDERS: dict[str, Callable[[Any, dict[str, Any]], AchievementSet | None]] = {}


def register(provider_id: str, fn: Callable[[Any, dict[str, Any]], AchievementSet | None]) -> None:
    _PROVIDERS[provider_id] = fn


def has_provider(provider_id: str) -> bool:
    return provider_id in _PROVIDERS


def fetch_for(provider_id: str, game: Any, ctx: dict[str, Any]) -> AchievementSet | None:
    """Dispatch to a registered provider; never raises (returns ``None``)."""
    import logging

    logger = logging.getLogger(__name__)
    fn = _PROVIDERS.get(provider_id)
    if fn is None:
        return None
    try:
        return fn(game, ctx)
    except Exception:  # noqa: BLE001 - one provider failing must not block others
        logger.exception("Achievements fetch failed for provider %s (%s)", provider_id, getattr(game, "name", ""))
        return None