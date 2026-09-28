"""Source providers: where games come from.

A source knows how to enumerate the games a user owns on it. Steam, GOG and
Epic will each add one; ``local`` covers games added by hand. Sources never
touch the GUI, so they can be synced from a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vitrine.services.artwork import FORCE_REFRESH_SETTING


@dataclass(frozen=True)
class SourceGame:
    """A game as reported by its store, before it becomes a library entry."""

    source: str
    appid: str
    name: str
    slug: str | None = None
    catalog_slug: str | None = None
    installed: bool = False
    runner: str = "wine"
    details: dict[str, Any] = field(default_factory=dict)


class Source:
    """Base class for a game source.

    Uses the template-method pattern: :meth:`sync` lays out the shared catalogue
    workflow and subclasses provide the owned-game list via :meth:`_fetch_games`
    (and optionally filter via :meth:`_filter_game`). Not an ``ABC`` because
    ``LocalSource`` intentionally has no catalogue and overrides ``sync``.
    """

    id: str = ""
    name: str = ""
    icon: str | None = None
    #: True if the source needs an account before it can list anything.
    requires_auth: bool = False
    #: Default artwork source for this store's games (provider vs lutris, ...).
    artwork_default: str = "lutris"
    #: Setting key holding the logged-in account id (empty for local).
    account_setting: str = ""

    def __init__(self, library: Any) -> None:
        self.library = library

    # -- catalogue sync (shared template) --------------------------------------

    def sync(self) -> int:
        """Refresh this source's games and merge them into the library.

        Shared template every store follows: clear the cached catalogue, read
        the owned games, upsert them into both the catalogue cache and the
        library's ``games`` table, prune entries that fell out, and promote
        legacy artwork defaults. Subclasses provide the owned-game list
        (:meth:`_fetch_games`) and can filter (:meth:`_filter_game`).
        """
        self.library.clear_source_games(self.id)

        deduped: dict[str, SourceGame] = {}
        for game in self._fetch_games():
            if not self._filter_game(game):
                continue
            deduped.setdefault(game.appid, game)

        for game in deduped.values():
            self.library.upsert_source_game(
                self.id,
                game.appid,
                game.name,
                slug=game.slug,
                catalog_slug=game.catalog_slug,
                installed=game.installed,
                **game.details,
            )

        self.library.merge_source_games(self.id, deduped.values())
        self.library.prune_source_games(self.id, deduped.keys())
        # Newly-synced entries use this source's default artwork source; rows
        # that predate the artwork feature still carry "local" -- promote them
        # so a later, async artwork pass knows to fetch.
        if self.artwork_default:
            for game in self.library.games(source=self.id):
                if (game.artwork_source or "") == "local":
                    game.artwork_source = self.artwork_default
                    self.library.update(game)
        return len(deduped)

    def _fetch_games(self) -> list[SourceGame]:
        """Return every owned game the store reports (pre-dedup, unfiltered)."""
        raise NotImplementedError

    def _filter_game(self, game: SourceGame) -> bool:
        """Whether ``game`` should be kept (e.g. exclude known non-games)."""
        return True

    def games_needing_artwork(self) -> list[Any]:
        """Return this source's games that still lack cached artwork.

        When the global "refresh artwork for all games" setting is enabled,
        every game (even ones with artwork) is returned so a source refresh
        re-pulls them all.
        """
        force = bool(self.library.setting(FORCE_REFRESH_SETTING, False))
        pending = []
        for game in self.library.games(source=self.id):
            if not (game.cover and game.banner) or force:
                pending.append(game)
        return pending

    # -- installed / auth ------------------------------------------------------

    def sync_installed(self) -> int:
        """Mark which of this source's games are installed locally.

        Sources that cannot tell return 0 and leave the flags alone.
        """
        return 0

    def is_configured(self) -> bool:
        """Whether the source has everything it needs to sync."""
        return True

    # -- auth lifecycle (shared by SyncService / the UI login flow) ------------

    def auth_store(self):
        """The durable credential store for the currently-known account.

        Returns ``None`` for sources that don't need an account (e.g. local).
        """
        if not self.requires_auth:
            return None
        return self.login_token_store()

    def remember_account(self) -> None:
        """Persist the account id discovered during login/sync, if any.

        Default is a no-op; stores that track a user/account id override this to
        write their ``account_setting``.
        """

    def clear_account(self) -> None:
        """Forget the remembered account id (set it to None)."""
        if self.account_setting and self.requires_auth:
            self.library.set_setting(self.account_setting, None)

    def logout(self) -> None:
        """Clear this source's stored credentials, if any."""
        store = self.auth_store()
        if store is not None:
            store.clear()

    def reset(self) -> None:
        """Drop credentials and the cached/relevant library rows for this source.

        Called by the UI's "Reset session" action. The source clears its credentials
        and removes owned-but-not-installed library rows (the authoritative
        installed set on disk is preserved).
        """
        self.logout()
        self.library.clear_source_games(self.id)
        on_disks = set(self.installed_on_disk()) if hasattr(self, "installed_on_disk") else set()
        self.library.prune_source_games(self.id, keep_installed=False, preserve_on_disk=on_disks)
        self.clear_account()


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
