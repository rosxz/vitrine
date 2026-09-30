"""Achievement providers: Steam, GOG, Epic.

Each provider exposes ``configured(ctx)`` and ``fetch(game, ctx)`` and registers
itself with :mod:`vitrine.services.achievement_providers.base` so the
orchestration layer can dispatch by provider id.
"""

from __future__ import annotations

from vitrine.services.achievement_providers import epic, gog, steam
from vitrine.services.achievement_providers.base import fetch_for as fetch_for
from vitrine.services.achievement_providers.base import has_provider as has_provider
from vitrine.services.achievement_providers.base import register as register

PROVIDER_IDS = ("steam", "gog", "epic")


def _register() -> None:
    register(steam.ID, steam.fetch)
    register(gog.ID, gog.fetch)
    register(epic.ID, epic.fetch)


_register()