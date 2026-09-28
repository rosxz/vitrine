"""Source abstractions (re-exported from the domain layer).

The ``Source`` ABC, :class:`SourceGame` DTO and :class:`SourceRegistry` live in
``vitrine.domain.source``; this module re-exports them so provider/UI code can
keep importing ``from vitrine.sources.base import ...`` or
``from vitrine.sources import registry`` unchanged.
"""

from __future__ import annotations

from vitrine.domain.source import Source, SourceGame, SourceRegistry, registry  # noqa: F401

__all__ = ["Source", "SourceGame", "SourceRegistry", "registry"]