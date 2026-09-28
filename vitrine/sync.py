"""Library sync / session orchestration across sources (GUI-free).

Consolidates the per-source refresh and reset flows that used to live, triplicated,
on the window. A :class:`SyncService` knows how to refresh a source and prune its
stale rows, and how to clear a source's session unconditionally. It never touches
GTK: the UI layer supplies toasts/artwork callbacks, and source *login* dialogs
are constructed by the UI (sources stay GUI-free).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .sources import registry
from .sources.base import Source

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Outcome of a catalogue refresh for one source."""

    source: str
    count: int = 0
    pending_artwork: int = 0


class AuthRequired(Exception):
    """Raised when a sync is attempted before the source is logged in."""

    def __init__(self, source: Source) -> None:
        self.source_id = source.id
        super().__init__(f"Sign in to {source.name} first (cog → {source.name})")


class SyncService:
    """Refreshing and resetting store sources without the window doing it by hand.

    ``on_toast`` is an optional callable taking a message string; the GUI passes it
    so progress/errors surface without the service knowing GTK.
    """

    def __init__(self, library, on_toast: Callable[[str], None] | None = None) -> None:
        self.library = library
        self._toast = on_toast

    def source(self, source_id: str) -> Source:
        """Return a fresh instance of ``source_id`` bound to the library."""
        cls = registry.get(source_id)
        if cls is None:
            raise ValueError(f"Unknown source: {source_id}")
        return cls(self.library)

    # -- refresh ---------------------------------------------------------------

    def sync(self, source_id: str, *, sync_installed: bool = True) -> SyncResult:
        """Refresh a source's catalogue and remember its account.

        Returns how many games were known and how many still need artwork. Does
        not touch the GUI; the caller decides what to do with the result.
        """
        source = self.source(source_id)
        if source.requires_auth and not source.is_authenticated():
            raise AuthRequired(source)

        count = source.sync()
        if sync_installed:
            source.sync_installed()
        source.remember_account()

        pending = source.games_needing_artwork()
        return SyncResult(source=source_id, count=count, pending_artwork=len(pending))

    # -- reset -----------------------------------------------------------------

    def reset(self, source_id: str) -> None:
        """Clear a source's credentials and stale library rows."""
        self.source(source_id).reset()