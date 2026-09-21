"""GOG offline-installer download (no Galaxy client required).

GOG's own public product API exposes the offline Windows installers for every
game::

    GET https://api.gog.com/products/<id>?expand=downloads
        -> downloads.installers[].files[].downlink   # a JSON endpoint
    GET <that downlink>   (Bearer auth)
        -> {"downlink": "<final file url>"}

We pick the largest Windows installer file, resolve its final download URL, and
download it -- keeping GOG installs fully self-contained (no GOG Galaxy client).
"""

from __future__ import annotations

import logging
import shutil
import urllib.request
from typing import Any

import requests

from .auth import GogAuthError, GogTokenStore

logger = logging.getLogger(__name__)

#: Public product metadata incl. download links (needs auth for owned games).
PRODUCT_URL = "https://api.gog.com/products/%s?expand=downloads"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def offline_installer(store: GogTokenStore, game_id: str, title: str = "") -> str:
    """Return the final download URL for the largest Windows offline installer.

    Raises :class:`GogAuthError` if unauthenticated, or ``ValueError`` if no
    Windows installer is available.
    """
    token = store.access_token()
    if not token:
        raise GogAuthError("GOG session expired — sign in again")
    product = _product(token, game_id)
    downlink_url = _pick_installer_downlink(product, title or game_id)
    return _resolve_downlink(token, downlink_url)


def _product(token: str, game_id: str) -> dict[str, Any]:
    response = requests.get(
        PRODUCT_URL % game_id,
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code == 401:
        raise GogAuthError("GOG session expired — sign in again")
    response.raise_for_status()
    return response.json()


def _pick_installer_downlink(product: dict[str, Any], title: str) -> str:
    """Pick the largest ``files[].downlink`` across Windows installers."""
    downloads = product.get("downloads") or {}
    installers = downloads.get("installers") or []
    best_url = ""
    best_size = -1
    for installer in installers:
        if not isinstance(installer, dict):
            continue
        if str(installer.get("os") or "").lower() not in ("windows", "win32", "win"):
            continue
        for file_ in installer.get("files") or []:
            if not isinstance(file_, dict):
                continue
            downlink = str(file_.get("downlink") or "")
            size = file_.get("size")
            size_int = int(size) if isinstance(size, int) else -1
            if downlink and size_int >= best_size:
                best_url = downlink
                best_size = size_int
    if not best_url:
        raise ValueError(f"No Windows offline installer available for {title}")
    return best_url


def _resolve_downlink(token: str, downlink_url: str) -> str:
    """Fetch the download-link JSON to obtain the real file URL."""
    response = requests.get(
        downlink_url,
        headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    final_url = payload.get("downlink") or ""
    if not final_url:
        raise ValueError("GOG returned no download link for this installer")
    return final_url


def download_installer(url: str, dest: str, chunk: int = 1 << 20) -> str:
    """Download an installer URL to ``dest``, returning the path."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst, chunk)
    return dest