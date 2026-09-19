"""Automatic game artwork from an external source.

Two sources are supported:

- **Lutris** -- the lutris.net game database, searched by the game's name (or a
  user-pinned ``lutris_slug``). Returns a portrait cover and a wide banner.
- **Provider** -- the game's own store (Steam CDN today, keyed by Steam appid),
  likewise a portrait cover and a wide banner.

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
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from . import paths
from .util import slugify

logger = logging.getLogger(__name__)

USER_AGENT = "Vitrine/0.1 (game library launcher; artwork metadata lookup)"

LUTRIS_API = "https://lutris.net/api/games"
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps/%s/%s"
STEAM_CDN_SHARED = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%s/%s"

#: Portrait cover ratio (width / height) used by the grid.
COVER_RATIO = 2 / 3
#: Wide banner ratio used by the hero detail bar.
BANNER_RATIO = 16 / 9

#: How many cached pixels a dimension may have; covers are small by design.
_SAFE_DIM = 640

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

def fetch(pair: tuple[str | list[str], str | list[str]], game: Any, force: bool = False) -> bool:
    """Download, crop and cache ``(cover_urls, banner_urls)`` for ``game``.

    Each slot is either a single URL string or a list of candidate URLs tried
    in order until one succeeds. Returns ``True`` if anything changed.
    ``game.cover``/``game.banner`` are updated in place to the cached paths.
    """
    cover_url, banner_url = pair
    key = game.lutris_slug or game.source_id or slugify(game.name) or game.id and str(game.id) or "game"
    cover_path = _save(cover_url, _cache_path(key, "cover"), force=force)
    banner_path = _save(banner_url, _cache_path(key, "banner"), force=force)

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


def _save(url_or_urls: str | list[str], dest: Path, force: bool = False) -> Path | None:
    """Download an image, resize to the target ratio, cache it.

    Accepts a single URL or a list of candidates tried in order; the first
    candidate that downloads and processes cleanly wins.
    """
    urls = [url_or_urls] if isinstance(url_or_urls, str) else list(url_or_urls)
    for url in urls:
        if not url:
            continue
        saved = _save_one(url, dest, force=force)
        if saved is not None:
            return saved
    return None


def _save_one(url: str, dest: Path, force: bool = False) -> Path | None:
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
        img.thumbnail((_SAFE_DIM, _SAFE_DIM), Image.LANCZOS)
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

def artwork_for(game: Any) -> tuple[str, str]:
    """Return ``(cover_url, banner_url)`` for a game based on its source.

    ``local`` games have no automatic source and return empty URLs (the user
    picks files). ``lutris`` games query the lutris.net database by pinned slug
    or name. ``provider`` games use the store's own art (Steam CDN by appid).
    """
    source = getattr(game, "artwork_source", "local") or "local"
    if source == "local":
        return "", ""
    if source == "lutris":
        slug = game.lutris_slug or pick_lutris_slug(game.name)
        if slug:
            try:
                return lutris_art(slug)
            except ArtworkError:
                return "", ""
        return "", ""
    if source == "provider":
        appid = game.source_id or ""
        if appid and game.source == "steam":
            return steam_art(appid)
        return "", ""
    return "", ""


def refresh_game_artwork(library: Any, game: Any, force: bool = False) -> bool:
    """Fetch and persist artwork for one game according to its source.

    Downloads/caches images, sets ``game.cover``/``game.banner`` in place and
    saves via ``library.update``. Returns ``True`` if something changed, so
    callers know to trigger a UI reload.
    """
    changed = fetch_game_artwork(game, force=force)
    if changed:
        library.update(game)
    return changed


def fetch_game_artwork(game: Any, force: bool = False) -> bool:
    """Download/cache a single game's artwork -- **no database access**.

    Safe to call from a worker thread: mutates only ``game.cover``/``game.banner``
    and returns ``True`` if either changed. The caller (usually the UI thread)
    decides when/how to persist with ``library.update``.
    """
    if getattr(game, "artwork_source", "local") == "local":
        return False  # manual; user manages files directly.
    urls = artwork_for(game)
    if not urls[0] and not urls[1]:
        return False  # nothing to fetch for this game.
    if not force and game.cover and game.banner:
        return False  # already has artwork cached.
    return fetch(urls, game, force=force)