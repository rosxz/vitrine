"""GOG as a Vitrine game source.

Mirrors the Steam source's design: an online store whose owned library (installed
or not) comes from the GOG web API, so login is required to fill the library.
Each game's identifier is GOG's numeric product ``id`` and its ``slug`` doubles
as the catalogue slug used for artwork and store links.

Unlike Steam, activating a GOG game launches it through the same local Wine
pipeline as a hand-added game (GOG games are ordinary Windows executables) --
there is no store-native launch URI.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from .. import paths
from ..artwork import FORCE_REFRESH_SETTING
from ..library import Game, Library
from ..util import slugify
from .base import Source, SourceGame, registry
from .gog.auth import GogAuthError, GogTokenStore

logger = logging.getLogger(__name__)

#: The owned-products listing endpooint. `GET /account/getFilteredProducts`
#: returns only products owned by the authenticated user.
OWNED_URL = "https://embed.gog.com/account/getFilteredProducts"
USER_DATA_URL = "https://embed.gog.com/userData.json"
#: Products per listing page (the API caps this).
PRODUCTS_PER_PAGE = 100

#: Setting key (string) holding the logged-in GOG user id.
USER_SETTING = "gog_user_id"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

WITHOUT_LOGIN_HINT = "Sign in to GOG to see your full library"


class GogSource(Source):
    id = "gog"
    name = "GOG"
    icon = "gog-galaxy"
    requires_auth = True

    def __init__(self, library: Library) -> None:
        self.library = library
        self.user_id = str(library.setting(USER_SETTING) or "")

    # -- Source API -----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.user_id) and self._token_store().exists()

    def login_token_store(self) -> GogTokenStore:
        return self._token_store()

    def is_authenticated(self) -> bool:
        return self._token_store().is_authenticated()

    def sync(self) -> int:
        """Refresh the GOG catalogue into the library.

        Returns how many known products were synced. Artwork is not downloaded
        here (it would block the UI for many games); callers fetch it
        asynchronously.
        """
        self.library.clear_source_games(self.id)

        games = self._all_games()

        deduped: dict[str, SourceGame] = {}
        for game in games:
            deduped[game.appid] = game

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
        # Drop products that fell out of the owned catalogue on this refresh.
        self.library.prune_source_games(self.id, deduped.keys())
        # GOG entries use Lutris artwork by default (no store-native cover URL).
        for game in self.library.games(source=self.id):
            if (game.artwork_source or "") == "local":
                game.artwork_source = "lutris"
                self.library.update(game)
        return len(deduped)

    def games_needing_artwork(self) -> list:
        pending = []
        force = bool(self.library.setting(FORCE_REFRESH_SETTING, False))
        for game in self.library.games(source=self.id):
            if not (game.cover and game.banner) or force:
                pending.append(game)
        return pending

    def sync_installed(self) -> int:
        """Reconcile GOG installed state purely from local disk (no store API).

        Vitrine calls this after a catalogue sync. It scans the gogdl depot root
        (``$XDG_DATA_HOME/vitrine/gog/<slug>/``) for ``goggame-*.info`` markers,
        upgrades rows whose game id appears on disk to *installed*, and repairs
        their executable when missing (best effort via ``gogdl import``). It only
        ever upgrades -- mirrors Steam's conservative behaviour and never flips an
        installed game back on a library refresh.
        """
        on_disk = self._scan_on_disk()
        updated = 0
        for game in self.library.games(source=self.id):
            root = on_disk.get(game.source_id or "")
            if root is None:
                continue
            changed = False
            if not game.installed:
                game.installed = True
                changed = True
            exe = game.executable
            if not exe or not os.path.isfile(os.path.expanduser(exe)):
                resolved = self._resolve_executable(game.source_id, root)
                if resolved:
                    game.executable = resolved
                    changed = True
            if changed and game.id is not None:
                self.library.update(game)
                updated += 1
        self._dedupe_installed_twins(on_disk)
        return updated

    def installed_on_disk(self) -> set[str]:
        """GOG game ids that are installed on this machine (gogdl depot markers)."""
        return set(self._scan_on_disk())

    def _scan_on_disk(self) -> dict[str, str]:
        """Map GOG game id -> install root from ``goggame-*.info`` markers.

        gogdl writes a depot to ``data_dir/gog/<slug>/[<InstallDir>/]`` and the
        marker ``goggame-<id>.info`` can sit directly in or one level below that
        root, so we search shallowly.
        """
        depot_root = paths.data_dir() / "gog"
        found: dict[str, str] = {}
        if not depot_root.is_dir():
            return found
        for slug_dir in depot_root.iterdir():
            if not slug_dir.is_dir():
                continue
            for info in filter(
                lambda p: p.is_file() and re.fullmatch(r"goggame-\d+\.info", p.name),
                slug_dir.rglob("goggame-*.info"),
            ):
                game_id = info.name[len("goggame-") : -len(".info")]
                found.setdefault(game_id, str(info.parent))
        return found

    def _resolve_executable(self, game_id: str, root: str) -> str | None:
        """Best-effort absolute executable for an on-disk GOG game."""
        from .gog import gogdl

        auth_path = str(paths.cache_dir() / "gogdl-auth.json")
        info = gogdl.import_info(game_id, root, auth_path)
        return gogdl.executable_from_info(info, root)

    def _dedupe_installed_twins(self, on_disk: dict[str, str]) -> int:
        """Drop non-installed duplicates when a truly-installed twin exists.

        A catalogue refresh can leave both an installed row (source_id matches a
        ``goggame-*.info`` on disk) and a stale owned-but-not-installed twin for
        the same title. The stale twin is the one users tend to click ("Play"
        opens install instead of launching). Remove it so one game = one tile.
        """
        installed_ids = set(on_disk)
        by_name: dict[str, list[Game]] = {}
        for game in self.library.games(source=self.id):
            by_name.setdefault(_norm_name(game.name), []).append(game)

        removed = 0
        for rows in by_name.values():
            if len(rows) < 2:
                continue
            installed = [g for g in rows if g.installed and g.source_id in installed_ids]
            if len(installed) != 1:
                continue
            keep_id = installed[0].id
            for duplicate in (g for g in rows if g.id != keep_id and not g.installed):
                self.library.remove(duplicate.id)
                removed += 1
        if removed:
            logger.info("Removed %d stale non-installed GOG duplicate(s)", removed)
        return removed

    # -- login / logout -------------------------------------------------------

    def save_cookies(self, cookies) -> None:
        store = self._token_store()
        store.set_credentials(cookies)

    def logout(self) -> None:
        self._token_store().clear()

    # -- game collection ------------------------------------------------------

    def _all_games(self) -> list[SourceGame]:
        token = self.ensure_fresh_token()
        return self._owned_games(token)

    def _user_data(self, token: str) -> dict:
        return _get_json(USER_DATA_URL, token)

    def _owned_games(self, token: str) -> list[SourceGame]:
        games: list[SourceGame] = []
        page = 0
        while True:
            payload = _get_json(
                OWNED_URL,
                token,
                params={
                    "mediaType": "1",  # games (not movies)
                    "hideLinuxGames": "0",
                    "page": str(page + 1),
                    "sortBy": "title",
                    "productsPerPage": str(PRODUCTS_PER_PAGE),
                },
            )
            products = payload.get("products") or []
            for item in products:
                game = self._game_from_product(item)
                if game is not None:
                    games.append(game)
            total_pages = int(payload.get("totalPages") or 0)
            if total_pages <= 0 or page + 1 >= total_pages:
                break
            page += 1
        return games

    def _game_from_product(self, item: dict) -> SourceGame | None:
        product = item.get("product") or item  # tolerate both wrap shapes
        if not isinstance(product, dict):
            return None
        game_id = product.get("id")
        title = product.get("title")
        if not game_id or not title:
            return None
        slug = product.get("slug") or slugify(title)
        # GOG's catalogue API does not report local install state. Keep whatever
        # Vitrine already decided (e.g. after running an offline installer) so a
        # library refresh doesn't silently flip an installed game back to owned-
        # but-not-installed.
        prior = self.library.game_by_source_id(self.id, str(game_id))
        installed = bool(prior and prior.installed)
        return SourceGame(
            source=self.id,
            appid=str(game_id),
            name=str(title),
            slug=slug,
            catalog_slug=slug,
            installed=installed,
            details={
                "store_url": f"https://www.gog.com/game/{slug}",
                "year": product.get("releaseTimestamp") if "releaseTimestamp" in product else None,
            },
        )

    # -- helpers --------------------------------------------------------------

    def _token_store(self) -> GogTokenStore:
        return GogTokenStore(paths.secret_dir(), self.user_id or "0")

    def ensure_fresh_token(self) -> str:
        """Return a known-fresh GOG access token, refreshing if necessary.

        If the cached token needs refresh (has a refresh_token and is near
        expiry), refreshes and persists. If no refresh_token is available (e.g.
        a stale pre-refresh login), clears credentials and raises
        :class:`GogAuthError` so the UI prompts the user to sign in again.
        """
        from .gog.auth import refresh_access_token

        store = self._token_store()
        if store.needs_refresh():
            try:
                payload = refresh_access_token(store.refresh_token())
            except GogAuthError:
                store.clear()
                raise
            store.apply_refreshed(payload)
        if not store.access_token():
            raise GogAuthError(WITHOUT_LOGIN_HINT)
        return store.access_token()


registry.register(GogSource)


def _norm_name(name: str) -> str:
    """Normalise a title for duplicate comparison."""
    return " ".join(str(name or "").casefold().split())


def _get_json(url: str, token: str, params: dict | None = None) -> dict:
    """GET ``url`` with a GOG bearer token, returning parsed JSON."""
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code == 401:
        raise GogAuthError("GOG session expired — sign in again")
    response.raise_for_status()
    return response.json()