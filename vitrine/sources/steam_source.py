"""Steam as a Vitrine game source.

Steam is an online store: the whole owned library (installed or not) only ever
comes from the Web API, so login is always required to fill the library. The
locally-installed games discovered from ``appmanifest_*.acf`` are merged over
the web list to mark them installed and add playtime, but they never substitute
for login.

The library therefore shows every owned game; ones you do not own locally (or
that are not installed) render translucent and open the store page when
activated. A per-source setting toggles whether the Steam Family shared library
is included.

All network access goes through a durable credential cache (see
``steam.auth``): the access token is reused until near expiry and refreshed in
the background without reopening a browser.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

import requests

from .. import paths
from ..artwork import FORCE_REFRESH_SETTING
from ..library import Library
from ..util import slugify
from .base import Source, SourceGame, registry
from .steam import config as steam_config
from .steam.auth import CookieJar, SteamAuthError, SteamTokenStore

logger = logging.getLogger(__name__)

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

#: Setting key (boolean) controlling whether the Steam Family shared library is
#: included in the owned list.
FAMILY_SETTING = "steam_include_family"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/84.0.4147.38 Safari/537.36"
)

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
SHARED_URL = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
FAMILY_URL = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"

WITHOUT_LOGIN_HINT = "Sign in to Steam to see your full library"


class SteamSource(Source):
    id = "steam"
    name = "Steam"
    icon = "steam-client"
    requires_auth = True

    def __init__(self, library: Library) -> None:
        self.library = library
        self.steam_root = steam_config.find_steam_root()
        self.steamid64 = steam_config.active_steamid64(self.steam_root) if self.steam_root else ""

    # -- Source API -----------------------------------------------------------

    def is_configured(self) -> bool:
        # Login is required; but the source is still "configured" if we can find
        # any Steam install or any cached account. Detect by token store.
        return bool(self._token_store().exists()) or bool(self.steam_root)

    def login_token_store(self) -> SteamTokenStore:
        return self._token_store()

    def is_authenticated(self) -> bool:
        store = self._token_store()
        return store.exists() and bool(store.access_token())

    def sync(self) -> int:
        """Refresh the Steam catalogue into the library.

        Writes both the source-games cache and the library's own ``games``
        table so every owned title (installed or not) shows in the unified
        grid. Returns how many known games were synced. Artwork is *not*
        downloaded here (it would block the UI for hundreds of games); callers
        fetch it asynchronously.
        """
        self.library.clear_source_games(self.id)

        games = self._all_games()

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

        self.library.merge_source_games(self.id, deduped.values())
        # Drop owned-but-not-installed entries that fell out of the catalogue
        # on this refresh (e.g. after a logout in a *different* Steam app).
        self.library.prune_source_games(self.id, deduped.keys())
        # Newly-synced Steam entries should use the store/provider artwork by
        # default; older rows that predate the artwork feature still carry the
        # old "local" default -- promote those to "provider" too so a later,
        # async artwork pass knows to fetch them.
        for game in self.library.games(source=self.id):
            if (game.artwork_source or "") == "local":
                game.artwork_source = "provider"
                self.library.update(game)
        return len(deduped)

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

    def sync_installed(self) -> int:
        """Merge locally-installed information over the web library."""
        installed = {game.appid: game for game in self._installed_games()}
        updated = 0
        for game in self.library.games(source=self.id):
            local = installed.get(game.source_id or "")
            if not local:
                continue
            if not game.installed:
                game.installed = True
                self.library.update(game)
                updated += 1
            playtime_seconds = _to_number(local.details.get("playtime_forever"))
            if playtime_seconds is not None:
                game.playtime = float(playtime_seconds) / 60.0
            lastplayed = _to_number(local.details.get("lastplayed"))
            if lastplayed is not None:
                game.lastplayed = lastplayed
        return updated

    def installed_on_disk(self) -> set[str]:
        """AppIDs that are actually installed locally (from app manifests)."""
        return {game.appid for game in self._installed_games()}

    # -- login / logout -------------------------------------------------------

    def save_cookies(self, cookies: CookieJar) -> None:
        """Store a freshly-captured browser session, then fetch the token."""
        store = self._token_store()
        store.set_credentials(cookies)
        store.fetch_access_token()

    def logout(self) -> None:
        self._token_store().clear()

    # -- game collection ------------------------------------------------------

    def _all_games(self) -> list[SourceGame]:
        store = self._token_store()
        installed = {g.appid: g for g in self._installed_games()}
        if not store.exists() or not store.access_token():
            raise SteamAuthError(WITHOUT_LOGIN_HINT)

        games = self._owned_games(store)
        # Mark and enrich entries that are installed locally (the dataclass is
        # frozen, so produce updated copies).
        merged: list[SourceGame] = []
        for game in games:
            local = installed.get(game.appid)
            if not local:
                merged.append(game)
                continue
            details = {**game.details, **local.details}
            merged.append(dataclasses.replace(game, installed=True, details=details))
        return merged

    def include_family(self) -> bool:
        return bool(self.library.setting(FAMILY_SETTING, True))

    def _owned_games(self, store: SteamTokenStore) -> list[SourceGame]:
        session = self._api_session(store)
        games = self._owned_page(session, store)
        if self.include_family():
            group = self._family_group(session, store)
            if group:
                games += self._family_page(session, store, group)
        return games

    # -- web API --------------------------------------------------------------

    def _api_session(self, store: SteamTokenStore) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        session.params = {"access_token": store.access_token()}
        return session

    def _owned_page(self, session: requests.Session, store: SteamTokenStore) -> list[SourceGame]:
        response = session.get(
            OWNED_URL,
            params={
                "key": store.access_token(),
                "steamid": store.steamid64 or self.steamid64,
                "format": "json",
                "include_appinfo": "1",
                "include_played_free_games": "1",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json().get("response") or {}
        games: list[SourceGame] = []
        for item in payload.get("games") or []:
            playtime = item.get("playtime_forever", 0)
            # The web API only tells us the game is owned/played; it cannot say
            # whether it is installed on THIS machine. Local installation is
            # figured out from the app manifests in _all_games/_installed_games,
            # so web entries always start as not-installed here.
            games.append(
                SourceGame(
                    source=self.id,
                    appid=str(item["appid"]),
                    name=item.get("name", str(item["appid"])),
                    slug=slugify(item.get("name", "")),
                    installed=False,
                    details={
                        "playtime_hours": _minutes_to_hours(playtime),
                        "lastplayed": item.get("rtime_last_played"),
                        "playtime_forever": playtime,
                        "time_last_played": item.get("rtime_last_played"),
                        "store_url": f"https://store.steampowered.com/app/{item['appid']}",
                    },
                )
            )
        return games

    def _family_group(self, session: requests.Session, store: SteamTokenStore) -> str | None:
        response = session.get(
            FAMILY_URL,
            params={"steamid": store.steamid64 or self.steamid64},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json().get("response") or {}
        if payload.get("is_not_member_of_any_group"):
            return None
        return payload.get("family_groupid")

    def _family_page(self, session: requests.Session, store: SteamTokenStore, group: str) -> list[SourceGame]:
        response = session.get(
            SHARED_URL,
            params={
                "family_groupid": group,
                "steamid": store.steamid64 or self.steamid64,
            },
            timeout=30,
        )
        response.raise_for_status()
        games: list[SourceGame] = []
        for item in response.json().get("response", {}).get("apps") or []:
            appid = str(item.get("appid"))
            games.append(
                SourceGame(
                    source=self.id,
                    appid=appid,
                    name=item.get("name", appid),
                    slug=slugify(item.get("name", "")),
                    details={"family_shared": True, "store_url": f"https://store.steampowered.com/app/{appid}"},
                )
            )
        return games

    # -- helpers --------------------------------------------------------------

    def _token_store(self) -> SteamTokenStore:
        return SteamTokenStore(paths.secret_dir(), self.steamid64 or "0")

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
                "playtime_hours": _minutes_to_hours(_to_number(state.get("playtime_forever"))),
                "playtime_forever": state.get("playtime_forever"),
                "size_on_disk": state.get("SizeOnDisk"),
                "state_flags": state.get("StateFlags"),
            },
        )

    # -- running game / playtime ----------------------------------------------

    def installed_game_dir(self, appid: str) -> str | None:
        """Absolute path to an installed game's directory, or ``None``."""
        if not self.steam_root:
            return None
        for steamapps in steam_config.steamapps_dirs(self.steam_root):
            for manifest_path in steam_config.appmanifest_paths(steamapps):
                state = self._manifest_state(manifest_path)
                if state is None or str(state.get("appid")) != str(appid):
                    continue
                installdir = state.get("installdir")
                if installdir:
                    return os.path.join(steamapps, "common", installdir)
        return None

    def read_manifest_playtime(self, appid: str) -> tuple[float | None, int | None]:
        """Return ``(playtime_hours, lastplayed_unix)`` from the local manifest.

        Steam writes ``playtime_forever`` (minutes) and ``LastPlayed`` to the app
        manifest when the game exits, so reading it shortly after the process
        stops gives the authoritative, freshly-updated playtime without needing a
        Web API refresh.
        """
        if not self.steam_root:
            return None, None
        for steamapps in steam_config.steamapps_dirs(self.steam_root):
            for manifest_path in steam_config.appmanifest_paths(steamapps):
                state = self._manifest_state(manifest_path)
                if state is None or str(state.get("appid")) != str(appid):
                    continue
                hours = _minutes_to_hours(_to_number(state.get("playtime_forever")))
                lastplayed = _to_number(state.get("LastPlayed"))
                return hours, lastplayed
        return None, None

    @staticmethod
    def _manifest_state(manifest_path: str) -> dict | None:
        from .steam.vdf import parse_vdf_file

        data = parse_vdf_file(manifest_path)
        state = data.get("AppState")
        return state if isinstance(state, dict) else None

    def web_playtime(self, appid: str) -> tuple[float | None, int | None]:
        """Authoritative playtime from the Steam cloud (``GetOwnedGames``).

        Some manifests never carry ``playtime_forever`` (e.g. recently-played
        Proton titles), so after a session ends we fall back to the Web API value
        ``playtime_forever``/``rtime_last_played``, which Steam updates on exit.
        Returns ``(hours, lastplayed)`` or ``(None, None)`` when unknown.
        """
        store = self._token_store()
        if not store.exists() or not store.access_token():
            return None, None
        for game in self._owned_page(self._api_session(store), store):
            if str(game.appid) == str(appid):
                return (
                    game.details.get("playtime_hours"),
                    game.details.get("lastplayed"),
                )
        return None, None


registry.register(SteamSource)


def _to_number(value) -> int | float | None:
    """Coerce a VDF/API value to a number, tolerating None/empty/non-numeric.

    Steam manifests store numbers as strings (``"0"``), but a field can also
    be absent (→ ``None``) or a dash; never pass those into ``int()``/``float()``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "--"):
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def _minutes_to_hours(minutes) -> float | None:
    """Steam reports playtime in whole minutes; Vitrine stores hours."""
    if minutes is None:
        return None
    return round(float(minutes) / 60.0, 2)