"""Epic Games as a Vitrine game source, via the legendary CLI.

Mirrors the Steam/GOG source design: a store whose owned library comes from the
account's cloud catalogue, surfaced here by ``legendary list``. Legendary is a
storeless client, so there is no store-natively-opened window during launch --
but the user may run the Epic Games Store client under Wine too, which the UI
manages via separate Launch/Focus/Kill store buttons.

Unlike Steam, launching an Epic game goes through legendary (or its resolved
dry-run command) rather than a store URI, and local installation state is read
from the EGS Wine-prefix manifests in addition to legendary's own list.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import paths
from ..library import Library
from ..util import slugify
from .base import Source, SourceGame, registry
from .epic import config as epic_config
from .epic import legendary as lg
from .epic.auth import EpicAuthError, EpicTokenStore

logger = logging.getLogger(__name__)

#: Setting key (string) holding the logged-in Epic account id.
ACCOUNT_SETTING = "epic_account_id"
#: Setting key (boolean): open the live debug log window for Epic operations.
EPIC_SHOW_DEBUG_SETTING = "epic_show_debug_log"

WITHOUT_LOGIN_HINT = "Sign in to Epic first (cog → Epic / Settings)"


class EpicSource(Source):
    id = "epic"
    name = "Epic"
    icon = "epic-games"
    requires_auth = True

    def __init__(self, library: Library) -> None:
        self.library = library
        self.account_id = str(library.setting(ACCOUNT_SETTING) or "")

    # -- Source API -----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.account_id) or bool(self._token_store().exists())

    def login_token_store(self) -> EpicTokenStore:
        return self._token_store()

    def is_authenticated(self) -> bool:
        # legendary has valid credentials, or our own token store does.
        try:
            if lg.is_installed() and self._has_legendary_credentials():
                return True
        except Exception:  # noqa: BLE001 - detection must never crash the UI
            pass
        return self._token_store().is_authenticated()

    def sync(self) -> int:
        """Refresh the Epic catalogue into the library.

        Mirrors Steam/GOG: merge the owned list into ``games``/``source_games``
        and prune entries that fell out. Artwork is not downloaded here; callers
        fetch it asynchronously.
        """
        if not self.is_authenticated():
            raise EpicAuthError(WITHOUT_LOGIN_HINT)

        self.library.clear_source_games(self.id)

        games = self._all_games()

        deduped: dict[str, SourceGame] = {}
        for game in games:
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
        # Epic titles use Lutris artwork  (no store-native cover here).
        for game in self.library.games(source=self.id):
            if (game.artwork_source or "") == "local":
                game.artwork_source = "lutris"
                self.library.update(game)
        return len(deduped)

    def games_needing_artwork(self) -> list[Any]:
        pending = []
        for game in self.library.games(source=self.id):
            if not (game.cover and game.banner):
                pending.append(game)
        return pending

    def sync_installed(self) -> int:
        """Mark installed Epic titles from both legendary and EGS manifests."""
        update = 0
        installed_ids = self.installed_on_disk()
        for game in self.library.games(source=self.id):
            if game.source_id in installed_ids and not game.installed:
                game.installed = True
                self.library.update(game)
                update += 1
        return update

    def installed_on_disk(self) -> set[str]:
        """App ids actually installed locally (legendary + EGS manifests)."""
        ids: set[str] = set()
        try:
            for game in lg.list_installed():
                app = game.get("app_name") or game.get("appName")
                if app:
                    ids.add(str(app))
        except (lg.LegendaryError, EpicAuthError):
            pass
        for manifest in epic_config.installed_manifests():
            app = manifest.get("AppName")
            if app:
                ids.add(str(app))
        return ids

    # -- login / logout -------------------------------------------------------

    def save_code(self, code: str, token: dict | None = None) -> None:
        """Record an OAuth exchange code, then import it into legendary."""
        if token:
            resolved = extract_account(token) or self.account_id or "unknown"
        else:
            resolved = self.account_id or "unknown"
        self.account_id = resolved
        self.library.set_setting(ACCOUNT_SETTING, resolved)
        real_store = EpicTokenStore(paths.secret_dir(), resolved)
        real_store.set_credentials(code, token)
        # Give legendary the code so it can authenticate for install/launch.
        if lg.is_installed():
            lg.auth(code)

    def logout(self) -> None:
        self._token_store().clear()
        if self.library.setting(ACCOUNT_SETTING):
            self.library.set_setting(ACCOUNT_SETTING, None)

    # -- game collection ------------------------------------------------------

    def _all_games(self) -> list[SourceGame]:
        # Prefer legendary's list when it is installed and configured.
        games: list[SourceGame] = []
        from .epic import auth as epic_auth

        try:
            legendary_items = lg.list_games()
            games = [self._from_legendary(item) for item in legendary_items]
        except (lg.LegendaryError, EpicAuthError) as exc:
            logger.info("Legendary listing unavailable (%s); falling back to HTTP", exc)
            try:
                token = self._token_store().access_token()
                if token:
                    items = epic_auth.list_owned(token)
                    games = [self._from_legendary(item) for item in items]
            except Exception as exc2:  # noqa: BLE001
                logger.warning("HTTP Epic listing also failed: %s", exc2)
        return [g for g in games if g is not None]

    def _from_legendary(self, item: dict) -> SourceGame | None:
        app_name = item.get("app_name") or item.get("appName")
        title = item.get("title")
        if not app_name or not title:
            return None
        slug = item.get("slug") or slugify(str(title))
        return SourceGame(
            source=self.id,
            appid=str(app_name),
            name=str(title),
            slug=slug,
            catalog_slug=slug,
            installed=bool(item.get("installed")),
            details={
                "store_url": f"https://store.epicgames.com/p/{slug}",
                "image": item.get("image"),
            },
        )

    # -- helpers --------------------------------------------------------------

    def _has_legendary_credentials(self) -> bool:
        return lg.is_installed() and epic_config.legendary_credentials_file().exists()

    def _token_store(self) -> EpicTokenStore:
        return EpicTokenStore(paths.secret_dir(), self.account_id or "unknown")


registry.register(EpicSource)


def extract_account(token: dict) -> str:
    """Best-effort account id from an Epic token payload."""
    for key in ("account_id", "accountId", "sub"):
        value = token.get(key)
        if isinstance(value, str) and value:
            return value
    return ""