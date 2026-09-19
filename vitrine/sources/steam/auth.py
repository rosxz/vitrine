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

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

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


class CookieJar:
    """An ordered cookie list that persists session cookies too.

    Unlike the standard library's ``MozillaCookieJar`` (whose load-filtering
    drops expiration-less session cookies), this keeps every cookie exactly as
    captured so stored credentials survive a restart.
    """

    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self.cookies: list[dict[str, Any]] = cookies or []

    def add(self, cookie: dict[str, Any]) -> None:
        self.cookies.append(cookie)

    def get(self, name: str, default: str = "") -> str:
        for cookie in reversed(self.cookies):
            if cookie.get("name") == name:
                return str(cookie.get("value", default))
        return default

    def to_dict(self) -> list[dict[str, Any]]:
        return self.cookies

    @classmethod
    def from_dict(cls, data: list[dict[str, Any]]) -> CookieJar:
        return cls(data)

    def expires(self, name: str) -> int | None:
        for cookie in self.cookies:
            if cookie.get("name") == name and cookie.get("expires"):
                try:
                    return int(cookie["expires"])
                except (TypeError, ValueError):
                    return None
        return None


class SteamTokenStore:
    """Reads and writes the durable credential file for one Steam account.

    The file is JSON so cookies with an absent ``expires`` field can be stored
    verbatim (the reason the relogin fix exists). Path layout:
    ``<secret_dir>/steam/auth_<steamid64>.json``.
    """

    def __init__(self, secret_dir: str | Path, steamid64: str) -> None:
        self.secret_dir = Path(secret_dir)
        self.steamid64 = steamid64
        self.filename = self.secret_dir / "steam" / f"auth_{steamid64}.json"
        #: Netscape-format cookie file that the login browser writes to.
        self.cookie_file = self.secret_dir / "steam" / f"cookies_{steamid64}.txt"

    # -- persistence ----------------------------------------------------------

    def load(self) -> dict[str, Any]:
        try:
            with open(self.filename, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.filename.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, self.filename)

    def exists(self) -> bool:
        return self.filename.is_file()

    def clear(self) -> None:
        try:
            self.filename.unlink(missing_ok=True)
        except FileNotFoundError:
            pass

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
        response = session.get(
            ACCESS_TOKEN_URL,
            cookies=_cookies_for_requests(cookies),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token_data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(token_data, dict):
            raise SteamAuthError("Unexpected access-token response")
        token = token_data.get("webapi_token")
        if not isinstance(token, str) or not token:
            raise SteamAuthError("No webapi_token in response")

        self.set_credentials(cookies, access_token=token)
        return token


def _cookies_for_requests(jar: CookieJar) -> dict[str, str]:
    """Flatten a CookieJar into a ``{name: value}`` map for requests."""
    return {cookie["name"]: str(cookie["value"]) for cookie in jar.to_dict()}


def read_netscape_cookies(path: str | Path) -> CookieJar:
    """Read a Netscape-format cookie file written by WebKit.

    Preserves session cookies (no ``expires`` field) so stored credentials
    survive a restart -- unlike :class:`http.cookiejar.MozillaCookieJar`, which
    would silently drop them.
    """
    jar = CookieJar()
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_") :]
                elif not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 7:
                    continue
                domain, _flag, path_elem, secure, expires, name, value = fields[:7]
                try:
                    expires_value = int(expires) if expires else None
                except ValueError:
                    expires_value = None
                jar.add(
                    {
                        "domain": domain,
                        "path": path_elem,
                        "secure": secure.lower() == "true",
                        "expires": expires_value,
                        "name": name,
                        "value": value,
                    }
                )
    except (OSError, ValueError):
        return CookieJar()
    return jar