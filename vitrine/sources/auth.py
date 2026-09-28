"""Shared credential/cookie persistence base (GUI-free).

Steam and GOG both keep an ordered cookie list (that survives session cookies)
and a durable JSON credential file per account. The classes here factor out the
identical persistence core so each store only adds its provider-specific token
accessors. Public aliases (:class:`CookieJar`, :class:`JsonCredentialStore`) are
re-exported and used by the store subclasses in ``steam/auth.py`` / ``gog/auth.py``
so call sites keep working unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


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


class JsonCredentialStore:
    """Reads and writes a per-account JSON credential file.

    Subclasses name the account id field and cookie jar type; persistence
    (atomic JSON write, path layout under ``<secret_dir>/<provider>/auth_<id>.json``)
    is shared so the login/browser flows behave identically.
    """

    provider: str = ""
    cookie_cls: type[CookieJar] = CookieJar

    def __init__(self, secret_dir: str | Path, account_id: str) -> None:
        self.secret_dir = Path(secret_dir)
        self.account_id = account_id
        self.filename = self.secret_dir / self.provider / f"auth_{account_id}.json"
        #: Netscape-format cookie file that the login browser writes to.
        self.cookie_file = self.secret_dir / self.provider / f"cookies_{account_id}.txt"

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