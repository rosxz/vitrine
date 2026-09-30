"""Steam achievements via the Steam Web API.

Two calls are needed:
- ``ISteamUserStats/GetSchemaForGame/v2`` -- the achievement definitions
  (name, display name, description, hidden flag, unlocked/locked icon URLs).
- ``ISteamUserStats/GetPlayerAchievements/v1`` -- the logged-in user's progress
  per achievement (``achieved`` 0/1 and ``unlocktime``).

Both require a Steam Web API key (free, user-supplied via the Settings →
Achievements page) and the user's SteamID64. ``GetPlayerAchievements`` needs the
profile's game-details to be public; if it errors we still expose the
definitions (all locked).
"""

from __future__ import annotations

from typing import Any

import requests

from vitrine.domain.achievement import Achievement, AchievementSet

ID = "steam"

PROVIDER_IDS = (ID,)

#: Context keys (populated by services.achievements.load_context).
API_KEY_CTX = "steam_api_key"
STEAMID_CTX = "steamid64"

API_ROOT = "https://api.steampowered.com"
TIMEOUT = 20


def configured(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get(API_KEY_CTX) and ctx.get(STEAMID_CTX))


def _get_schema(appid: str, api_key: str) -> dict[str, Any] | None:
    url = f"{API_ROOT}/ISteamUserStats/GetSchemaForGame/v2/"
    try:
        response = requests.get(url, params={"key": api_key, "appid": appid}, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):  # noqa: BLE001
        return None


def _get_player_achievements(appid: str, steamid: str, api_key: str) -> dict[str, Any] | None:
    url = f"{API_ROOT}/ISteamUserStats/GetPlayerAchievements/v1/"
    try:
        response = requests.get(
            url, params={"key": api_key, "steamid": steamid, "appid": appid, "l": "english"}, timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):  # noqa: BLE001
        return None


def fetch(game: Any, ctx: dict[str, Any]) -> AchievementSet | None:
    appid = str(getattr(game, "source_id", "") or "")
    if not appid:
        return None
    api_key = str(ctx.get(API_KEY_CTX) or "")
    steamid = str(ctx.get(STEAMID_CTX) or "")
    if not configured(ctx):
        return None

    schema = _get_schema(appid, api_key)
    if not schema:
        return None
    stats = schema.get("game", {}).get("availableGameStats", {}) or {}
    definitions = stats.get("achievements") or []
    if not definitions:
        return None

    # User progress (may be unavailable for private profiles -> all locked).
    progress: dict[str, dict[str, Any]] = {}
    playerstats = _get_player_achievements(appid, steamid, api_key)
    if playerstats:
        for item in playerstats.get("playerstats", {}).get("achievements") or []:
            progress[str(item.get("apiname"))] = item

    achievements: list[Achievement] = []
    for idx, definition in enumerate(definitions):
        name = str(definition.get("name") or "")
        unlocked = bool(progress.get(name, {}).get("achieved"))
        unlock_time = progress.get(name, {}).get("unlocktime")
        achievements.append(
            Achievement(
                key=name,
                name=str(definition.get("displayName") or name),
                description=str(definition.get("description") or ""),
                hidden=bool(definition.get("hidden")),
                unlocked=unlocked,
                unlock_date=int(unlock_time) if unlock_time else None,
                progress=1.0 if unlocked else 0.0,
                icon_locked_url=definition.get("icongray"),
                icon_unlocked_url=definition.get("icon"),
                sort=idx,
            )
        )
    return AchievementSet.build(provider=ID, items=achievements)