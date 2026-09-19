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

import requests

from .. import paths
from ..library import Library
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
        for game in self.library.games(source=self.id):
            if not (game.cover and game.banner):
                pending.append(game)
        return pending

    def sync_installed(self) -> int:
        # GOG games are ordinary executables; installed-ness is managed like
        # local entries (the per-game executable field), so there is nothing
        # store-native to merge here.
        return 0

    def installed_on_disk(self) -> set[str]:
        return set()

    # -- login / logout -------------------------------------------------------

    def save_cookies(self, cookies) -> None:
        store = self._token_store()
        store.set_credentials(cookies)

    def logout(self) -> None:
        self._token_store().clear()

    # -- game collection ------------------------------------------------------

    def _all_games(self) -> list[SourceGame]:
        store = self._token_store()
        token = store.access_token()
        if not token:
            raise GogAuthError(WITHOUT_LOGIN_HINT)
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
        return SourceGame(
            source=self.id,
            appid=str(game_id),
            name=str(title),
            slug=slug,
            catalog_slug=slug,
            details={
                "store_url": f"https://www.gog.com/game/{slug}",
                "year": product.get("releaseTimestamp") if "releaseTimestamp" in product else None,
            },
        )

    # -- helpers --------------------------------------------------------------

    def _token_store(self) -> GogTokenStore:
        return GogTokenStore(paths.secret_dir(), self.user_id or "0")


registry.register(GogSource)


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