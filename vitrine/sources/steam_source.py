"""Steam as a Vitrine game source.

Two capabilities, both off the UI thread:

- **Installed** games are discovered without any login, from Steam's local
  ``appmanifest_<appid>.acf`` files. This always works, even offline.
- **Owned / Family** library lists require the store's access token. When a
  valid token is cached (see ``steam.auth``), the web API is queried for owned
  games (``GetOwnedGames``) and, if the user is in a Steam Family group, the
  shared library too.

The credential cache reuses an unexpired token and only re-fetches it when it
is nearly expired, so the user is not asked to log in again on every launch.
"""

from __future__ import annotations

import requests

from ..library import Library
from ..paths import secret_dir
from ..util import slugify
from .base import Source, SourceGame, registry
from .steam import config as steam_config
from .steam.auth import SteamTokenStore

#: Excluded Steam tool apps that are not games.
EXCLUDED_APPIDS = {
    "221410",  # Steamworks Common Redistributables
    "228980",  # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1070561",  # Steam Linux Runtime 2.0 (sniper) - old id
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1493710",  # Proton Experimental
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "2180100",  # Proton Hotfix
    "2805730",  # Proton 9.0
    "4183110",  # Steam Linux Runtime 4.0
    "4628710",  # Proton 11.0
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
SHARED_URL = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
FAMILY_URL = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"


class SteamSource(Source):
    id = "steam"
    name = "Steam"
    icon = "steam-client"

    def __init__(self, library: Library) -> None:
        self.library = library
        self.steam_root = steam_config.find_steam_root()
        self.steamid64 = steam_config.active_steamid64(self.steam_root) if self.steam_root else ""

    # -- Source API -----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.steam_root)

    def sync(self) -> int:
        """Refresh the Steam catalogue into the library's source-games cache."""
        self.library.clear_source_games(self.id)

        games = self._installed_games()
        if self._has_access_token():
            games.extend(self._web_games())

        deduped: dict[str, SourceGame] = {}
        for game in games:
            if game.appid in EXCLUDED_APPIDS:
                continue
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
        return len(deduped)

    def sync_installed(self) -> int:
        """Mark locally-installed Steam games as installed in the library."""
        installed_appids = {game.appid for game in self._installed_games()}
        updated = 0
        for parsed in self.library.games(source=self.id):
            if parsed.source_id in installed_appids and not parsed.installed:
                parsed.installed = True
                self.library.update(parsed)
                updated += 1
            elif parsed.source_id not in installed_appids and parsed.installed:
                parsed.installed = False
                self.library.update(parsed)
                updated += 1
        return updated

    # -- local discovery ------------------------------------------------------

    def _installed_games(self) -> list[SourceGame]:
        if not self.steam_root:
            return []
        games: list[SourceGame] = []
        for steamapps in steam_config.steamapps_dirs(self.steam_root):
            for manifest_path in steam_config.appmanifest_paths(steamapps):
                game = self._game_from_manifest(manifest_path)
                if game is not None:
                    games.append(game)
        return games

    def _game_from_manifest(self, manifest_path: str) -> SourceGame | None:
        from .steam.vdf import parse_vdf_file

        data = parse_vdf_file(manifest_path)
        state = data.get("AppState")
        if not isinstance(state, dict):
            return None
        appid = state.get("appid")
        name = state.get("name")
        if not appid or not name:
            return None

        return SourceGame(
            source=self.id,
            appid=str(appid),
            name=str(name),
            slug=slugify(str(name)),
            installed=True,
            details={
                "installdir": state.get("installdir"),
                "lastplayed": state.get("LastPlayed"),
                "playtime_forever": state.get("playtime_forever"),
                "size_on_disk": state.get("SizeOnDisk"),
                "state_flags": state.get("StateFlags"),
            },
        )

    # -- web API --------------------------------------------------------------

    def _has_access_token(self) -> bool:
        if not self.steamid64:
            return False
        store = SteamTokenStore(secret_dir(), self.steamid64)
        if not store.exists():
            return False
        token = store.access_token()
        return bool(token) and not store.age_seconds() > 3600 * 26

    def _web_games(self) -> list[SourceGame]:
        """Fetch owned + family games, requiring a cached valid token."""
        if not self.steamid64:
            return []
        store = SteamTokenStore(secret_dir(), self.steamid64)
        token = store.access_token()
        if not token:
            return []
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

        games: list[SourceGame] = []
        response = session.get(
            OWNED_URL,
            params={
                "key": token,
                "steamid": self.steamid64,
                "format": "json",
                "include_appinfo": "1",
                "include_played_free_games": "1",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json().get("response") or {}
        for item in payload.get("games") or []:
            playtime = item.get("playtime_forever", 0)
            games.append(
                SourceGame(
                    source=self.id,
                    appid=str(item["appid"]),
                    name=item.get("name", str(item["appid"])),
                    slug=slugify(item.get("name", "")),
                    details={
                        "playtime_forever": playtime,
                        "time_last_played": item.get("rtime_last_played"),
                        "store_url": f"https://store.steampowered.com/app/{item['appid']}",
                    },
                )
            )

        family_group = self._family_group(session, store)
        if family_group:
            shared = session.get(
                SHARED_URL,
                params={
                    "access_token": token,
                    "family_groupid": family_group,
                    "steamid": self.steamid64,
                },
                timeout=30,
            )
            shared.raise_for_status()
            owned = {g.appid for g in games}
            for item in shared.json().get("response", {}).get("apps") or []:
                appid = str(item.get("appid"))
                if appid in owned:
                    continue
                games.append(
                    SourceGame(
                        source=self.id,
                        appid=appid,
                        name=item.get("name", appid),
                        slug=slugify(item.get("name", "")),
                        details={"family_shared": True},
                    )
                )
        return games

    @staticmethod
    def _family_group(session: requests.Session, store: SteamTokenStore) -> str | None:
        response = session.get(
            FAMILY_URL,
            params={
                "access_token": store.access_token(),
                "steamid": store.steamid64,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json().get("response") or {}
        if payload.get("is_not_member_of_any_group"):
            return None
        return payload.get("family_groupid")


registry.register(SteamSource)