"""GOG offline-installer download (no Galaxy client required).

Mirrors the approach of community GOG downloaders: the owned game's download
metadata comes from the authenticated ``getGamesData`` endpoint, which lists the
Windows offline installers. We pick the largest ``Windows`` installer and
download it via GOG's ``downlink`` redirect, returning a local path the caller
can hand to the configured Wine/Proton prefix. This keeps GOG installs fully
self-contained -- no Epic Games / GOG Galaxy client.
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Any

import requests

from .auth import GogAuthError, GogTokenStore

logger = logging.getLogger(__name__)

#: Per-game download metadata (needs auth).
GAMES_DATA_URL = "https://www.gog.com/account/getGamesData"
#: Redirects to the actual installer file.
DOWNLINK_URL = "https://www.gog.com/downlink"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def offline_installer(store: GogTokenStore, game_id: str, title: str = "") -> str:
    """Return the largest Windows offline-installer download URL for ``game_id``.

    Raises :class:`GogAuthError` if unauthenticated, or ``ValueError`` if no
    Windows installer is available.
    """
    token = store.access_token()
    if not token:
        raise GogAuthError("GOG session expired — sign in again")
    payload = _game_data(token, game_id)
    installer_url = _pick_installer(payload, title or game_id)
    return f"{DOWNLINK_URL}/{installer_url.lstrip('/')}?token={token}"


def _game_data(token: str, game_id: str) -> dict[str, Any]:
    response = requests.post(
        GAMES_DATA_URL,
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
        json={"gameIds": [int(game_id)],
              "generator": "Galaxy",
              "platform": "windows"},
        timeout=30,
    )
    if response.status_code == 401:
        raise GogAuthError("GOG session expired — sign in again")
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or payload
    game = data.get(str(game_id)) if isinstance(data, dict) else None
    if game is None:
        # Some responses nest under "gameIds".
        for record in (data.get("gameIds") or []) if isinstance(data, dict) else []:
            if str(record.get("id")) == str(game_id):
                game = record
                break
    return game or {}


def _pick_installer(payload: dict[str, Any], title: str) -> str:
    """Pick the offline installer from ``getGamesData`` download metadata."""
    downloads = payload.get("downloads") or {}
    for key in ("windows", "Windows"):
        entries = downloads.get(key) or []
        best_path = ""
        best_size = -1
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Prefer the offline installer (not a patch/bonus).
            manual = str(entry.get("manualUrl") or "")
            install_type = str(entry.get("type") or "").lower()
            if install_type and "installer" not in install_type:
                continue
            size = entry.get("size")
            size_int = int(size) if isinstance(size, int) else -1
            if manual and size_int >= best_size:
                best_path = manual
                best_size = size_int
        if best_path:
            return best_path
    raise ValueError(f"No Windows offline installer available for {title}")


def download_installer(url: str, dest: str, chunk: int = 1 << 20) -> str:
    """Download an installer URL to ``dest``, returning the path."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler()
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=60) as src, open(dest, "wb") as dst:
        import shutil

        shutil.copyfileobj(src, dst, chunk)
    return dest