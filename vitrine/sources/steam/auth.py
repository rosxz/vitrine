"""Durable Steam authentication cache.

The root cause of "I have to log in again every time" is how the access
credential is cached. Lutris stores cookies in WebKit's Netscape format and
silently *drops* cookies that carry no expiry (session cookies) when reloading
them; the access token is stored separately with no expiry bookkeeping, so the
app cannot tell a still-valid session from an expired one and forces a fresh
browser login instead of renewing silently.

Vitrine fixes this by keeping, per Steam account:

- the login cookies verbatim (session cookies with no ``expires`` field are
  preserved, not discarded),
- the ``webapi_token`` (a short-lived app token) with the timestamp it was
  fetched, and
- the ``steamRefresh_steam`` cookie, a long-lived JWT that genuine Steam
  clients use to renew the short access token without re-entering credentials.

A token is reused until it is about to expire; only then is a network refresh
attempted. A refresh never reopens a browser -- that happens only when the
refresh credential itself is gone or rejected.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from vitrine.sources.auth import CookieJar, JsonCredentialStore

logger = logging.getLogger(__name__)

#: Minimum remaining lifetime (seconds) for a cached token to be trusted
#: without refreshing. Tokens are minted server-side; keeping a small margin
#: avoids a failed call racing the expiry.
TOKEN_GRACE_SECONDS = 300

#: Endpoint that returns the short-lived ``webapi_token`` for a logged-in
#: browser session (the same one Lutris' Steam Family service uses).
ACCESS_TOKEN_URL = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SteamAuthError(Exception):
    """Raised when Steam authentication state prevents an operation."""


class SteamTokenStore(JsonCredentialStore):
    """Reads and writes the durable credential file for one Steam account.

    The file is JSON so cookies with an absent ``expires`` field can be stored
    verbatim (the reason the relogin fix exists). Path layout:
    ``<secret_dir>/steam/auth_<steamid64>.json``.
    """

    provider = "steam"
    cookie_cls = CookieJar

    def __init__(self, secret_dir: str | Path, steamid64: str) -> None:
        super().__init__(secret_dir, steamid64)
        #: Keep the familiar attribute name available to call sites.
        self.steamid64 = steamid64

    # -- state accessors ------------------------------------------------------

    def set_credentials(self, cookies: CookieJar, access_token: str = "", fetched_at: int | None = None) -> None:
        self.save(
            {
                "steamid64": self.steamid64,
                "cookies": cookies.to_dict(),
                "access_token": access_token,
                "fetched_at": fetched_at if fetched_at is not None else int(time.time()),
            }
        )

    def cookies(self) -> CookieJar:
        return CookieJar.from_dict(self.load().get("cookies") or [])

    def access_token(self) -> str:
        return str(self.load().get("access_token") or "")

    def fetched_at(self) -> int:
        try:
            return int(self.load().get("fetched_at") or 0)
        except (TypeError, ValueError):
            return 0

    def age_seconds(self) -> int:
        return int(time.time()) - self.fetched_at()

    def refresh_token(self) -> str:
        """The durable browser refresh credential, if stored."""
        return self.cookies().get("steamRefresh_steam")

    def needs_network_refresh(self, access_token_ttl: int) -> bool:
        """Whether the cached access token is too old to trust."""
        return self.age_seconds() + TOKEN_GRACE_SECONDS > access_token_ttl

    # -- web token fetch ------------------------------------------------------

    def fetch_access_token(self) -> str:
        """Request a fresh access token for the stored session.

        Uses the persisted cookies; does not open a browser. Raises
        :class:`SteamAuthError` if the session credentials are missing or
        rejected.
        """
        cookies = self.cookies()
        if not cookies.to_dict():
            raise SteamAuthError("No cached Steam session to refresh")

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        # Steam's store AJAX endpoints expect a Referer from the same origin.
        session.headers["Referer"] = "https://store.steampowered.com/"
        session.headers["X-Requested-With"] = "XMLHttpRequest"
        response = session.get(
            ACCESS_TOKEN_URL,
            cookies=_cookies_for_requests(cookies),
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:  # noqa: BLE001
            logger.warning("Access-token endpoint returned non-JSON: %r", response.text[:200])
            raise SteamAuthError(f"Steam did not return JSON ({response.status_code})") from exc

        token = _extract_webapi_token(payload)
        if not token:
            logger.warning(
                "Access-token response had no webapi_token: %s",
                str(payload)[:300],
            )
            raise SteamAuthError("No webapi_token in response")

        self.set_credentials(cookies, access_token=token)
        return token


def _extract_webapi_token(payload: Any) -> str:
    """Dig a ``webapi_token`` out of Steam's store-AJAX shapes.

    Expected: ``{"success": true, "data": {"webapi_token": "..."}}`` (Lutris').
    Tolerate the token at the top level or nested one level deeper.
    """
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    for candidate in (data, payload):
        if isinstance(candidate, dict):
            token = candidate.get("webapi_token")
            if isinstance(token, str) and token:
                return token
    return ""


def _cookies_for_requests(jar: CookieJar):
    """Build a real domain-aware cookie jar for ``requests``.

    Lutris passes a genuine jar (WebkitCookieJar); a bare ``{name: value}``
    dict is wrong here because Steam sets ``sessionid`` on both
    ``store.steampowered.com`` and ``steamcommunity.com`` -- flattening them
    would collapse one of the pair and send each cookie to every host, which
    breaks the store session the token endpoint needs.
    """
    from http.cookiejar import Cookie

    out = requests.cookies.RequestsCookieJar()
    for entry in jar.to_dict():
        # requests needs a domain with the leading dot removed to match.
        domain = str(entry.get("domain") or "").lstrip(".")
        if not domain:
            continue
        out.set_cookie(
            Cookie(
                version=0,
                name=str(entry.get("name")),
                value=str(entry.get("value")),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=bool(entry.get("domain")),
                path=str(entry.get("path") or "/"),
                path_specified=True,
                secure=bool(entry.get("secure")),
                expires=entry.get("expires") or None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return out