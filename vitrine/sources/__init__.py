"""Source package: importing it registers every available source."""

from . import local  # noqa: F401  (imported for its registration side effect)
from .base import Source, SourceGame, registry

__all__ = ["Source", "SourceGame", "registry"]
