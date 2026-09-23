"""Automatic game artwork from external sources.

Providers:

- **Auto** (default) -- a priority chain configured in Settings → Appearance.
  tile slot walks (default) IGDB → SteamGridDB → Steam → Lutris; hero slot walks
  SteamGridDB → IGDB → Steam → Lutris. Providers missing an API key are skipped.
- **Provider** -- the game's own store (Steam CDN today, keyed by Steam appid).
- **IGDB / SteamGridDB** -- explicit single-provider choices (keys required).
- **Lutris** -- the lutris.net game database, searched by name (or a pinned
  ``lutris_slug``).

Images are downloaded, cropped to the two fixed aspect ratios Vitrine expects
(portrait cover and wide banner) and cached under ``~/.cache/vitrine/covers``.
Local/manual artwork bypasses this fetcher entirely.

Legal note: lutris.net's ``/api`` path is intentionally excluded from its
robots.txt, so this is not a *clearly* sanctioned public API. It is used here
with a self-identifying User-Agent, cached forever once fetched, and only
queried when a title actually needs art -- please reconsider if the project
ever scales beyond casual use.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from . import artwork_providers as providers
from . import paths
from .artwork_providers import igdb, steamgriddb
from .artwork_providers.base import Art, CandidateSet
from .util import slugify

logger = logging.getLogger(__name__)

USER_AGENT = "Vitrine/0.1 (game library launcher; artwork metadata lookup)"

LUTRIS_API = "https://lutris.net/api/games"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps/%s/%s"
STEAM_CDN_SHARED = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%s/%s"

# --- provider credentials & priority (settings keys) -------------------------

#: Setting key: IGDB Twitch Client-ID (string).
IGDB_CLIENT_ID_SETTING = "igdb_client_id"
#: Setting key: IGDB Twitch Client-Secret (string).
IGDB_CLIENT_SECRET_SETTING = "igdb_client_secret"
#: Setting key: SteamGridDB API key (string).
STEAMGRIDDB_KEY_SETTING = "steamgriddb_api_key"
#: Setting key: ordered tile-provider priority (JSON list of provider ids).
TILE_PRIORITY_SETTING = "artwork_tile_priority"
#: Setting key: ordered hero-provider priority (JSON list of provider ids).
HERO_PRIORITY_SETTING = "artwork_hero_priority"
#: Setting key (boolean): whether the "configure a provider key" hint was shown.
ARTWORK_HINT_SETTING = "artwork_provider_hint_seen"
#: Setting key (int): max cached width/height (px) for tile covers. ``None``/0 → default.
TILE_DIM_SETTING = "artwork_tile_dim"
#: Setting key (int): max cached width/height (px) for hero banners.
HERO_DIM_SETTING = "artwork_hero_dim"
#: Setting key (boolean): when on, a source refresh re-pulls artwork for EVERY
#: game, even ones that already have cached covers/banners (the normal default is
#: to skip games that already have artwork).
FORCE_REFRESH_SETTING = "refresh_all_artwork"

#: Env overrides for the provider keys (wind/fork: the flake may export these).
ENV_IGDB_CLIENT_ID = "VITRINE_IGDB_CLIENT_ID"
ENV_IGDB_CLIENT_SECRET = "VITRINE_IGDB_CLIENT_SECRET"
ENV_STEAMGRIDDB_KEY = "VITRINE_STEAMGRIDDB_KEY"

#: Default provider priority per slot; overridable in Settings → Appearance.
DEFAULT_TILE_PRIORITY = ("igdb", "steamgriddb", "steam", "lutris")
DEFAULT_HERO_PRIORITY = ("steamgriddb", "igdb", "steam", "lutris")

SOURCES = ("local", "auto", "lutris", "provider", "igdb", "steamgriddb")

#: Portrait cover ratio (width / height) used by the grid.
COVER_RATIO = 2 / 3
#: Wide banner ratio used by the hero detail bar.
BANNER_RATIO = 16 / 9

#: How many cached pixels a dimension may have; covers are small by design.
_SAFE_DIM = 640
#: Wide banners are shown large in the hero detail, so cache them at a higher
#: resolution (16:9 → 1280x720) to avoid visible softness from over-downscaling.
_BANNER_SAFE_DIM = 1280

#: How many HTTP requests may be sent before throttling kicks in.
_RATE_LIMIT_EVERY = 512
#: Additional seconds of sleep added for each throttled batch (1st, 2nd, ...).
_RATE_LIMIT_STEP = 10.0


class ArtworkError(Exception):
    """Raised when artwork cannot be fetched for a game."""


class _RateLimiter:
    """Throttle an external API/art host during large back-fills.

    Tracks request volume and, after every ``_RATE_LIMIT_EVERY`` requests,
    pauses for a duration that grows with each batch -- so a 700-game back-fill
    does not trip the host's rate limiter while still recovering quickly on
    small runs. ``call()`` is thread-safe (multiple art workers share it).
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._count = 0
        self._batches = 0

    def acquire(self) -> None:
        import time

        with self._lock:
            self._count += 1
            if self._count >= _RATE_LIMIT_EVERY:
                self._count = 0
                self._batches += 1
                sleep_for = _RATE_LIMIT_STEP * self._batches
            else:
                return
        # Sleep outside the lock so other workers keep making progress.
        logger.info("Artwork rate-limit pause: %.0fs (batch %d)", sleep_for, self._batches)
        time.sleep(sleep_for)


_RATE_LIMITER = _RateLimiter()


def _get(url: str, timeout: int = 20) -> requests.Response:
    _RATE_LIMITER.acquire()
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


# -- lutris.net ----------------------------------------------------------------

def lutris_search(name: str) -> list[dict[str, Any]]:
    """Search lutris.net for a game by name."""
    payload = _get(f"{LUTRIS_API}?{_urlencode({'search': name, 'page_size': 10})}").json()
    return [g for g in payload.get("results", []) if g.get("slug")]


def lutris_game(slug: str) -> dict[str, Any]:
    """Fetch a single Lutris game record by its slug."""
    return _get(f"{LUTRIS_API}/{slug}").json()


def lutris_art(slug: str) -> tuple[str, str]:
    """Return ``(cover_url, banner_url)`` for a Lutris slug.

    Uses the database's canonical portrait cover and wide banner; both are
    already the correct orientations for Vitrine's two slots.
    """
    game = lutris_game(slug)
    cover = game.get("coverart")
    banner = game.get("banner_url")
    if not cover and not banner:
        raise ArtworkError(f"No artwork for Lutris slug '{slug}'")
    return _as_url(cover) or "", _as_url(banner) or ""


def _as_url(value: Any) -> str:
    return str(value) if value else ""


# -- provider (Steam CDN) ------------------------------------------------------

def steam_art(appid: str) -> tuple[list[str], list[str]]:
    """Return ``(cover_urls, banner_urls)`` candidates for a Steam appid.

    Returns lists so the downloader can try each in turn until one succeeds:
    some storflanz titles are missing individual store assets, so the portrait
    cover falls back from the hi-res library capsule to the square header, both
    cropped to Vitrine's portrait ratio by ``_save``.
    """
    covers = [
        STEAM_CDN % (appid, "library_600x900_2x.jpg"),
        STEAM_CDN_SHARED % (appid, "library_600x900_2x.jpg"),
        STEAM_CDN % (appid, "library_600x900.jpg"),
        STEAM_CDN % (appid, "header.jpg"),
        STEAM_CDN % (appid, "capsule_616x353.jpg"),
    ]
    banners = [
        STEAM_CDN % (appid, "capsule_616x353.jpg"),
        STEAM_CDN_SHARED % (appid, "capsule_616x353.jpg"),
        STEAM_CDN % (appid, "header.jpg"),
    ]
    return covers, banners


# -- download + cache ----------------------------------------------------------

def fetch(
    pair: tuple[str | list[str], str | list[str]],
    game: Any,
    force: bool = False,
    dims: tuple[int | None, int | None] | None = None,
) -> bool:
    """Download, crop and cache ``(cover_urls, banner_urls)`` for ``game``.

    Each slot is either a single URL string or a list of candidate URLs tried
    in order until one succeeds. Returns ``True`` if anything changed.
    ``game.cover``/``game.banner`` are updated in place to the cached paths.

    ``dims`` is ``(tile_max, hero_max)`` in pixels; ``None`` uses the built-in
    defaults. Either way the cached image is never upscaled beyond its source,
    so a large value simply means "keep as much of the source as it has".
    """
    cover_url, banner_url = pair
    tile_dim, hero_dim = (dims or (None, None))
    key = game.lutris_slug or game.source_id or slugify(game.name) or game.id and str(game.id) or "game"
    cover_path = _save(cover_url, _cache_path(key, "cover"), force=force, dim=tile_dim)
    banner_path = _save(banner_url, _cache_path(key, "banner"), force=force, dim=hero_dim)

    changed = False
    if cover_path:
        game.cover = str(cover_path)
        changed = True
    if banner_path:
        game.banner = str(banner_path)
        changed = True
    return changed


def _cache_path(key: str, kind: str) -> Path:
    return Path(paths.covers_dir()) / f"{key}--{kind}-.jpg"


def _save(url_or_urls: str | list[str], dest: Path, force: bool = False, dim: int | None = None) -> Path | None:
    """Download an image, resize to the target ratio, cache it.

    Accepts a single URL or a list of candidates tried in order; the first
    candidate that downloads and processes cleanly wins. ``dim`` caps the cached
    longest dimension (never upscales past the source); ``None`` uses the kind's
    default resolution.
    """
    urls = [url_or_urls] if isinstance(url_or_urls, str) else list(url_or_urls)
    for url in urls:
        if not url:
            continue
        saved = _save_one(url, dest, force=force, dim=dim)
        if saved is not None:
            return saved
    return None


def _save_one(url: str, dest: Path, force: bool = False, dim: int | None = None) -> Path | None:
    """Download one ``url``, resize it to the cached ratio, cache it."""
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = _get(url, timeout=60)
    except (requests.RequestException, ArtworkError) as exc:
        logger.debug("Image download failed %s: %s", url, exc)
        return None
    try:
        img = Image.open(__import__("io").BytesIO(data.content))
        ratio = COVER_RATIO if "--cover-" in dest.name else BANNER_RATIO
        img = img.convert("RGB")
        w, h = img.size
        if w / h > ratio:
            new_w = h * ratio
            img = img.crop(((w - new_w) / 2, 0, (w + new_w) / 2, h))
        else:
            new_h = w / ratio
            img = img.crop((0, (h - new_h) / 2, w, (h + new_h) / 2))
        limit = dim or (_SAFE_DIM if "--cover-" in dest.name else _BANNER_SAFE_DIM)
        img.thumbnail((limit,) * 2, Image.LANCZOS)
        img.save(dest, "JPEG", quality=88)
    except Exception as exc:  # noqa: BLE001 - a bad image must not block syncing.
        logger.warning("Image processing failed for %s: %s", url, exc)
        return None
    return dest


def _urlencode(params: dict[str, Any]) -> str:
    import urllib.parse

    return urllib.parse.urlencode(params)


# -- slug helpers --------------------------------------------------------------

def pick_lutris_slug(name: str) -> str:
    """Best-effort Lutris slug for a game name, else an empty string."""
    try:
        results = lutris_search(name)
    except (requests.RequestException, ArtworkError):
        return ""
    if not results:
        # Fall back to the slugified name; some titles work verbatim.
        return slugify(name)
    return str(results[0].get("slug") or slugify(name))


# -- orchestrator ---------------------------------------------------------------

def artwork_for(game: Any, library: Any = None, ctx: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(cover_url, banner_url)`` for a game based on its source.

    ``local`` games have no automatic source and return empty URLs (the user
    picks files). ``auto`` (the default) walks the Settings → Appearance provider
    priority chain. ``lutris`` queries the lutris.net database by pinned slug or
    name. ``provider`` uses the store's own art (Steam CDN by appid). ``igdb`` /
    ``steamgriddb`` use that single provider (keys required).

    ``ctx`` is an :func:`load_context` snapshot made on the main thread so worker
    threads don't touch the library's sqlite connection. When omitted it is read
    here (safe on the main thread only).
    """
    if ctx is None:
        ctx = load_context(library)
    creds: dict[str, str] = ctx["creds"]
    source = (getattr(game, "artwork_source", "auto") or "auto").lower()
    if source == "local":
        return "", ""
    if source == "auto":
        return _automatic_pair(game, library, ctx)
    if source == "lutris":
        return _lutris_cover_banner(game)
    if source == "provider":
        return _provider_cover_banner(game)
    if source == "igdb":
        if igdb.configured(creds["igdb_client_id"], creds["igdb_client_secret"]):
            buckets = igdb.candidates(creds["igdb_client_id"], creds["igdb_client_secret"], game)
            return (_slot_urls(buckets.get("tile")), _slot_urls(buckets.get("hero")))
        return "", ""
    if source == "steamgriddb":
        key = creds["steamgriddb_key"]
        if steamgriddb.configured(key):
            return (
                _slot_urls(steamgriddb.tiles(key, game)),
                _slot_urls(steamgriddb.heroes(key, game)),
            )
        return "", ""
    return "", ""


def _lutris_cover_banner(game: Any) -> tuple[str, str]:
    slug = getattr(game, "lutris_slug", "") or pick_lutris_slug(getattr(game, "name", ""))
    if slug:
        try:
            return lutris_art(slug)
        except ArtworkError:
            return "", ""
    return "", ""


def _provider_cover_banner(game: Any) -> tuple[str, str]:
    appid = game.source_id or ""
    if appid and getattr(game, "source", "") == "steam":
        return steam_art(appid)
    return "", ""


def _slot_urls(arts: list[Art]) -> list[str]:
    return [a.url for a in arts]


def _automatic_pair(
    game: Any, library: Any = None, ctx: dict[str, Any] | None = None
) -> tuple[str, str]:
    if ctx is None:
        ctx = load_context(library)
    creds: dict[str, str] = ctx["creds"]
    # Lutris/Steam always contribute (no key). Offer the one-time hint when the
    # richer providers (IGDB/SteamGridDB) are unconfigured; the chain still runs
    # and falls through to whatever is available.
    if not (_igdb_configured(creds) or _steamgriddb_configured(creds)):
        mark_provider_hint_shown(library)
    cset = providers.candidates_for(game, creds)
    cover = _first_slot(cset, ctx["tile_priority"], "tile")
    hero = _first_slot(cset, ctx["hero_priority"], "hero")
    return cover, hero


def _igdb_configured(creds: dict[str, str]) -> bool:
    return igdb.configured(creds["igdb_client_id"], creds["igdb_client_secret"])


def _steamgriddb_configured(creds: dict[str, str]) -> bool:
    return steamgriddb.configured(creds["steamgriddb_key"])


def _first_slot(cset: CandidateSet, priority: list[str], slot: str) -> list[str] | str:
    """The first provider (by ``priority``) that has ``slot`` candidates → its list."""
    for provider_id in priority:
        arts = cset.by_provider.get(provider_id, {}).get(slot) or []
        if arts:
            return _slot_urls(arts)
    return ""


def user_art_pairs(cset: CandidateSet) -> dict[str, dict[str, list[Art]]]:
    """Group a CandidateSet by provider → slot → candidates (for the picker)."""
    return cset.by_provider


# --- credentials & provider priority -----------------------------------------

def load_credentials(library: Any = None) -> dict[str, str]:
    """Merge env + settings into an credentials dict for the providers."""
    creds = {
        "igdb_client_id": os.environ.get(ENV_IGDB_CLIENT_ID) or "",
        "igdb_client_secret": os.environ.get(ENV_IGDB_CLIENT_SECRET) or "",
        "steamgriddb_key": os.environ.get(ENV_STEAMGRIDDB_KEY) or "",
    }
    if library is not None:
        for setting_key, cred_key in (
            (IGDB_CLIENT_ID_SETTING, "igdb_client_id"),
            (IGDB_CLIENT_SECRET_SETTING, "igdb_client_secret"),
            (STEAMGRIDDB_KEY_SETTING, "steamgriddb_key"),
        ):
            if not creds[cred_key]:
                value = library.setting(setting_key)
                if value:
                    creds[cred_key] = str(value)
    return creds


def load_priority(library: Any = None) -> tuple[list[str], list[str]]:
    """Return ``(tile_priority, hero_priority)`` from settings or defaults."""
    tile = list(DEFAULT_TILE_PRIORITY)
    hero = list(DEFAULT_HERO_PRIORITY)
    if library is not None:
        stored = library.setting(TILE_PRIORITY_SETTING)
        if isinstance(stored, list) and stored:
            tile = _clean_priority(stored)
        stored = library.setting(HERO_PRIORITY_SETTING)
        if isinstance(stored, list) and stored:
            hero = _clean_priority(stored)
    return tile, hero


def _clean_priority(ids: list[Any]) -> list[str]:
    clean: list[str] = []
    for pid in ids:
        pid = str(pid).lower()
        if pid in providers.PROVIDER_IDS and pid not in clean:
            clean.append(pid)
    # Always keep configured providers available even if listed after others.
    for pid in providers.PROVIDER_IDS:
        if pid not in clean:
            clean.append(pid)
    return clean


def load_context(library: Any = None) -> dict[str, Any]:
    """Snapshot credentials + provider priority on the calling (main) thread.

    Worker threads cannot touch the library's sqlite connection (it is owned by
    the thread that created it), so read everything the artwork pipeline needs
    up front and pass this context into :func:`artwork_for`,
    :func:`provider_candidates` etc. instead of letting them read the library.
    """
    creds = load_credentials(library)
    tile_priority, hero_priority = load_priority(library)
    tile_dim = _read_dim(library, TILE_DIM_SETTING)
    hero_dim = _read_dim(library, HERO_DIM_SETTING)
    force_refresh = bool(library.setting(FORCE_REFRESH_SETTING, False)) if library is not None else False
    return {
        "creds": creds,
        "tile_priority": tile_priority,
        "hero_priority": hero_priority,
        "tile_dim": tile_dim,
        "hero_dim": hero_dim,
        "force_refresh": force_refresh,
    }


def _read_dim(library: Any, key: str) -> int | None:
    if library is None:
        return None
    value = library.setting(key)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _ctx_creds(ctx: dict[str, Any] | None, library: Any) -> dict[str, str]:
    return (ctx or {}).get("creds") or load_credentials(library)


def provider_hint_pending(library: Any) -> bool:
    """Whether to surface the one-time "configure an artwork provider" hint.

    True before either IGDB or SteamGridDB is configured *and* the hint hasn't
    been dismissed yet. Made explicit that lutris/steam still function without
    keys, so only the richer providers gate the hint.
    """
    if library is None:
        return False
    creds = load_credentials(library)
    if _igdb_configured(creds) or _steamgriddb_configured(creds):
        return False
    return not bool(library.setting(ARTWORK_HINT_SETTING, False))


def mark_provider_hint_shown(library: Any) -> None:
    """Record that the provider-configure hint was shown (idempotent)."""
    if library is not None:
        library.set_setting(ARTWORK_HINT_SETTING, True)


def provider_candidates(library: Any, game: Any, ctx: dict[str, Any] | None = None) -> CandidateSet:
    """Enumerate every candidate across configured providers (for the picker)."""
    return providers.candidates_for(game, _ctx_creds(ctx, library))


def set_art_for_game(library: Any, game: Any, cover: str, banner: str) -> None:
    """Persist explicitly-chosen ``cover``/``banner`` file paths onto ``game``."""
    game.cover = cover
    game.banner = banner
    if getattr(game, "id", None) is not None:
        library.update(game)


def refresh_game_artwork(library: Any, game: Any, force: bool = False, ctx: dict[str, Any] | None = None) -> bool:
    """Fetch and persist artwork for one game according to its source.

    Downloads/caches images, sets ``game.cover``/``game.banner`` in place and
    saves via ``library.update``. Returns ``True`` if something changed, so
    callers know to trigger a UI reload. Call on the main thread (or pass an
    existing ``ctx`` snapshot for use off-thread)."""
    changed = fetch_game_artwork(game, force=force, library=library, ctx=ctx)
    if changed:
        library.update(game)
    return changed


def fetch_game_artwork(game: Any, force: bool = False, library: Any = None, ctx: dict[str, Any] | None = None) -> bool:
    """Download/cache a single game's artwork -- **no database access**.

    Safe to call from a worker thread: mutates only ``game.cover``/``game.banner``
    and returns ``True`` if either changed. The caller (usually the UI thread)
    decides when/how to persist with ``library.update``.
    """
    if getattr(game, "artwork_source", "auto") == "local":
        return False  # manual; user manages files directly.
    # The global "refresh all artwork" option overrides the skip-already-cached
    # behaviour for the automatic source-refresh flow.
    force = force or bool((ctx or {}).get("force_refresh"))
    urls = artwork_for(game, library=library, ctx=ctx)
    if not urls[0] and not urls[1]:
        return False  # nothing to fetch for this game.
    if not force and game.cover and game.banner:
        return False  # already has artwork cached.
    dims = None
    if ctx is not None:
        dims = (ctx.get("tile_dim"), ctx.get("hero_dim"))
    return fetch(urls, game, force=force, dims=dims)