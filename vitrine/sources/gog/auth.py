"""Durable GOG authentication cache.

Mirrors the design of ``vitrine.sources.steam.auth``: credentials are persisted
to a per-account JSON file so the user only signs in once. GOG uses an OAuth2
code flow -- the embedded browser signs in at ``auth.gog.com`` then redirects to
``embed.gog.com/on_login_success`` carrying a one-time ``code``; the code is
exchanged for a short-lived bearer token, which authorizes every API call via an
``Authorization: Bearer <token>`` header.

A token is reused until it is about to expire; the ``refresh_token`` from the
first exchange renews it without re-opening a browser (mirroring Steam's
steamRefresh behaviour).

Path layout: ``<secret_dir>/gog/auth_<user_id>.json``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

#: OAuth2 client credentials used by the GOG Galaxy client and web app.
GOG_CLIENT_ID = "46899977096215655"
GOG_CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"

#: OAuth2 endpoints (see the unofficial gogapidocs).
AUTH_URL = "https://auth.gog.com/auth"
TOKEN_URL = "https://auth.gog.com/token"
#: Where the browser returns after login, carrying the one-time ``code``. GOG
#: requires the exact same value in both the auth request and the token exchange,
#: so it must not be altered between the two calls.
AUTH_REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"

#: Minimum remaining lifetime (seconds) for the cached token to be trusted.
TOKEN_GRACE_SECONDS = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class GogAuthError(Exception):
    """Raised when GOG authentication state prevents an operation."""


class GogCookieJar:
    """An ordered cookie list that persists session cookies too."""

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
    def from_dict(cls, data: list[dict[str, Any]]) -> GogCookieJar:
        return cls(data)


#: Cookies that together indicate a fully-authenticated GOG web session.
REQUIRED_COOKIES = ("gog_lci", "gog_us")


class GogTokenStore:
    """Reads and writes the durable credential file for one GOG account."""

    def __init__(self, secret_dir: str | Path, user_id: str) -> None:
        self.secret_dir = Path(secret_dir)
        self.user_id = user_id
        self.filename = self.secret_dir / "gog" / f"auth_{user_id}.json"
        #: Netscape-format cookie file that the login browser writes to.
        self.cookie_file = self.secret_dir / "gog" / f"cookies_{user_id}.txt"

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

    def set_credentials(self, cookies: GogCookieJar, access_token: str = "", fetched_at: int | None = None) -> None:
        self.save(
            {
                "user_id": self.user_id,
                "cookies": cookies.to_dict(),
                "access_token": access_token,
                "fetched_at": fetched_at if fetched_at is not None else int(time.time()),
            }
        )

    def cookies(self) -> GogCookieJar:
        return GogCookieJar.from_dict(self.load().get("cookies") or [])

    def access_token(self) -> str:
        return str(self.load().get("access_token") or "")

    def fetched_at(self) -> int:
        try:
            return int(self.load().get("fetched_at") or 0)
        except (TypeError, ValueError):
            return 0

    def age_seconds(self) -> int:
        return int(time.time()) - self.fetched_at()

    def is_authenticated(self) -> bool:
        return bool(self.access_token())

    def user_name(self) -> str:
        return self.load().get("user_name") or self.user_id or ""


def exchange_code_for_token(code: str) -> dict:
    """Exchange a one-time login ``code`` for a GOG bearer token.

    GOG's ``/token`` endpoint accepts a **POST** with URL-encoded form data
    (a GET against it returns 400 Bad Request). The ``redirect_uri`` must match
    the value used to start the login flow exactly.
    """
    data = {
        "client_id": GOG_CLIENT_ID,
        "client_secret": GOG_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": AUTH_REDIRECT_URI,
    }
    response = requests.post(TOKEN_URL, data=data, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("access_token"):
        raise GogAuthError("No access_token in token response")
    return payload


def _cookies_for_requests(jar: GogCookieJar):
    """Build a real, domain-aware cookie jar for ``requests``."""
    from http.cookiejar import Cookie

    out = requests.cookies.RequestsCookieJar()
    for entry in jar.to_dict():
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