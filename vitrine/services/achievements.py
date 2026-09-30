"""Achievements orchestration: credentials snapshot, fetch, icon caching.

Mirrors the artwork service: :func:`load_context` snapshots everything the
provider needs on the calling (main) thread -- because the library's sqlite
connection is main-thread-only and GOG token refresh mutates the token store --
and workers call :func:`fetch_achievements` which never touches the library.
Icons are downloaded and downscaled into :func:`paths.achievements_dir`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from vitrine.domain.achievement import (
    ACHIEVEMENTS_SOURCE_AUTO,
    ACHIEVEMENTS_SOURCE_NONE,
    Achievement,
    AchievementSet,
)

logger = logging.getLogger(__name__)

#: Setting key (str): Steam Web API key for reading Steam achievements.
STEAM_API_KEY_SETTING = "steam_web_api_key"
#: Setting key (bool): refresh achievements after a game exits.
AUTO_REFRESH_SETTING = "achievements_auto_refresh"

#: Environment overrides (mirror artwork's VITRINE_* handling).
ENV_STEAM_API_KEY = "VITRINE_STEAM_API_KEY"
ENV_GOG_COMET = "VITRINE_COMET"

#: Safe characters for cached icon filenames.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: Provider id used when a source has no own provider (future RetroAchievements).
_RA_PROVIDER = "retroachievements"


def _steam_api_key(library: Any) -> str:
    if library is not None:
        stored = library.setting(STEAM_API_KEY_SETTING)
        if stored:
            return str(stored)
    return os.environ.get(ENV_STEAM_API_KEY, "")


def load_context(library: Any = None) -> dict[str, Any]:
    """Snapshot credentials + auth state on the main thread for workers.

    Refreshes the GOG token here (mutates GogTokenStore); Steam reads its API
    key; Epic just needs legendary's session (no extra key). ``library`` may be
    ``None`` in tests, in which case only environment overrides are honoured.
    """
    ctx: dict[str, Any] = {
        "steam_api_key": _steam_api_key(library),
        "steamid64": None,
        "gog_access_token": None,
        "gog_user_id": None,
        "epic_available": True,
        "auto_refresh": bool(library.setting(AUTO_REFRESH_SETTING, True)) if library is not None else False,
    }
    _fill_accounts(ctx, library)
    return ctx


def _fill_accounts(ctx: dict[str, Any], library: Any) -> None:
    if library is None:
        return
    # Steam account id (steamid64) lives in the source's account setting.
    from vitrine.sources.steam_source import SteamSource

    try:
        steam = SteamSource(library)
        ctx["steamid64"] = getattr(steam, "steamid64", None)
    except Exception:  # noqa: BLE001 - best effort
        pass
    # GOG: refresh the token on the main thread, then read access token + user id.
    try:
        from vitrine.sources.gog_source import GogSource

        gog = GogSource(library)
        try:
            gog.ensure_fresh_token()
            store = gog.login_token_store()
            ctx["gog_access_token"] = store.access_token()
            ctx["gog_user_id"] = gog.user_id
        except Exception:  # noqa: BLE001 - not authenticated is fine
            pass
    except Exception:  # noqa: BLE001
        pass
    # Epic: legendary availability (not strictly required; it reports a session).
    try:
        from vitrine.sources.epic import legendary as lg

        ctx["epic_available"] = lg.is_installed()
    except Exception:  # noqa: BLE001
        pass


def provider_for(game: Any) -> str | None:
    """The achievement provider id for a game, or ``None`` if none applies."""
    source = getattr(game, "achievements_source", ACHIEVEMENTS_SOURCE_AUTO) or ACHIEVEMENTS_SOURCE_AUTO
    if source == ACHIEVEMENTS_SOURCE_NONE:
        return None
    if source == _RA_PROVIDER:
        return _RA_PROVIDER
    # "auto": provider follows the game's store source.
    return _store_provider(getattr(game, "source", "local") or "local")


def _store_provider(source: str) -> str | None:
    return source if source in ("steam", "gog", "epic") else None


def fetch_achievements(game: Any, ctx: dict[str, Any] | None = None, library: Any = None) -> AchievementSet | None:
    """Fetch a game's achievements. ``ctx`` may be a load_context() snapshot.

    If ``ctx`` is ``None`` (main thread call), builds one. Returns ``None`` when
    the game's source/provider has no achievements or can't run.
    """
    from vitrine.services.achievement_providers import base as providers

    provider = provider_for(game)
    if provider is None or not providers.has_provider(provider):
        return None
    if provider == _RA_PROVIDER:
        return None  # not implemented
    ctx = ctx or load_context(library)
    if not _configured(provider, ctx):
        return None
    return providers.fetch_for(provider, game, ctx)


def _configured(provider: str, ctx: dict[str, Any]) -> bool:
    from vitrine.services.achievement_providers import gog as gog_mod
    from vitrine.services.achievement_providers import steam as steam_mod

    if provider == "steam":
        return steam_mod.configured(ctx)
    if provider == "gog":
        return gog_mod.configured(ctx)
    return True  # epic / others


def refresh_game_achievements(library: Any, game: Any, ctx: dict[str, Any] | None = None) -> bool:
    """Fetch + persist a game's achievements. Call on the main thread.

    Downloads/caches icons, replaces stored rows and refreshes the cached summary
    on the game. Returns ``True`` if anything changed (so the UI can reload).
    """
    from vitrine.services.achievement_providers import base as providers

    provider = provider_for(game)
    if provider is None:
        return False
    ctx = ctx or load_context(library)
    result = providers.fetch_for(provider, game, ctx) if providers.has_provider(provider) else None
    if result is None or not result.achievements:
        return False
    _cache_icons(game, result)
    _persist(library, game, result)
    return True


def _persist(library: Any, game: Any, achievement_set: AchievementSet) -> None:
    # The game object may be a worker-side copy; reconcile on the library's.
    target = None
    if library is not None and getattr(game, "id", None) is not None:
        try:
            target = library.game(game.id)
        except Exception:  # noqa: BLE001
            target = None
    target = target or game
    library.replace_achievements(target, achievement_set)


def _cache_icons(game: Any, achievement_set: AchievementSet) -> None:
    """Download + downscale achievement icons into the per-game cache dir."""
    from vitrine.infra import paths

    game_id = getattr(game, "id", None)
    if game_id is None:
        return
    base = paths.achievements_dir() / str(game_id)
    base.mkdir(parents=True, exist_ok=True)
    for ach in achievement_set.achievements:
        unlocked = _download_icon(base, ach, unlocked=True)
        locked = _download_icon(base, ach, unlocked=False)
        object.__setattr__(ach, "icon_unlocked_path", unlocked)
        object.__setattr__(ach, "icon_locked_path", locked)


def _download_icon(base, ach: Achievement, *, unlocked: bool) -> str | None:
    url = ach.icon_unlocked_url if unlocked else ach.icon_locked_url
    if not url:
        return None
    ext = _extension(url)
    filename = f"{_SAFE.sub('_', ach.key or 'ach')}_{'un' if unlocked else 'lock'}{ext}"
    dest = base / filename
    if dest.exists():
        return str(dest)
    data = _fetch_icon(url)
    if data is None:
        return None
    try:
        data = _normalise(data)
    except Exception:  # noqa: BLE001
        pass
    with open(dest, "wb") as handle:
        handle.write(data)
    return str(dest)


def _fetch_icon(url: str) -> bytes | None:
    from vitrine.services.artwork_providers import net

    return net.get_bytes(url)


def _extension(url: str) -> str:
    lowered = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url else ""
    return f".{lowered}" if lowered in ("png", "jpg", "jpeg", "webp", "gif") else ".jpg"


def _normalise(data: bytes) -> bytes:
    """Downscale/encode to a compact JPEG using Pillow (same as artwork)."""
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(data))
    image = image.convert("RGB")
    image.thumbnail((256, 256))
    out = BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def newly_unlocked(old: AchievementSet | None, new: AchievementSet | None) -> list[Achievement]:
    """Return achievements that were locked/hidden in ``old`` but unlocked in ``new``."""
    if not old or not new:
        return []
    by_key = {a.key: a for a in old.achievements}
    fresh = []
    for ach in new.achievements:
        prev = by_key.get(ach.key)
        if ach.unlocked and (prev is None or not prev.unlocked):
            fresh.append(ach)
    return fresh