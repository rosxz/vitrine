"""Durable Epic authentication cache.

Mirrors the design of ``vitrine.sources.gog.auth``: the embedded login browser
completes Epic's OAuth flow and returns a one-time ``authorizationCode`` at the
``localhost`` redirect URI; that code is exchanged for a bearer token, which is
persisted per-account under ``<secret_dir>/epic/auth_<account_id>.json``. The
token also authorises legendary's own credential store (see
``legendary.auth --code``), so games can be installed and launched.

Path layout: ``<secret_dir>/epic/auth_<account_id>.json``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

#: Client credentials used by the Epic Games Launcher web login (from legendary).
#: The id and secret form the HTTP Basic auth for the OAuth token exchange.
EPIC_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
EPIC_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"

#: Epic's account login page with a baked-in redirect target. Mirrors Lutris'
#: EGS service: after the user signs in, Epic redirects to the ``/id/api/redirect``
#: URI, whose response body carries the JSON ``{"authorizationCode": "..."}``.
#: This avoids the fragile ``window.ue`` JS bridge entirely.
EPIC_AUTH_URL = (
    "https://www.epicgames.com/id/login?redirectUrl="
    "https%3A//www.epicgames.com/id/api/redirect%3F"
    "clientId%3D34a02cf8f4414e29b15921876da36f9a%26responseType%3Dcode"
)
#: Epic redirects here with the login code after a successful sign-in.
EPIC_REDIRECT_PREFIX = "https://www.epicgames.com/id/api/redirect"
#: OAuth token exchange endpoint (legendary's ``account/api/oauth/token``).
EPIC_TOKEN_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
#: Returns the authenticated account's basic info.
EPIC_ACCOUNT_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/verify"
#: Epic library service listing owned items (mirrors Lutris' EGS service).
EPIC_LIBRARY_URL = "https://library-service.live.use1a.on.epicgames.com"
#: Epic catalog service resolving an owned record's metadata (title, etc).
EPIC_CATALOG_URL = "https://catalog-public-service-prod06.ol.epicgames.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class EpicAuthError(Exception):
    """Raised when Epic authentication state prevents an operation."""


class EpicTokenStore:
    """Reads and writes the durable credential file for one Epic account."""

    def __init__(self, secret_dir: str | Path, account_id: str) -> None:
        self.secret_dir = Path(secret_dir)
        self.account_id = account_id
        self.filename = self.secret_dir / "epic" / f"auth_{account_id}.json"

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

    def set_credentials(self, code: str, token: dict | None = None) -> None:
        self.save(
            {
                "account_id": self.account_id,
                "code": code,
                "token": token or {},
            }
        )

    def code(self) -> str:
        return str(self.load().get("code") or "")

    def access_token(self) -> str:
        return str((self.load().get("token") or {}).get("access_token") or "")

    def is_authenticated(self) -> bool:
        return bool(self.access_token()) or bool(self.code())


def exchange_code_for_token(code: str) -> dict:
    """Exchange an Epic **exchange code** for a bearer token.

    The ``window.ue`` bridge hands us an exchange code (the primary legendary
    login path). Mirrors ``start_session(exchange_token=...)``:
    ``grant_type=exchange_code``, ``exchange_code=<code>``, ``token_type=eg1``,
    HTTP Basic auth.
    """
    data = {
        "grant_type": "exchange_code",
        "exchange_code": code,
        "token_type": "eg1",
    }
    return _token_request(data, "Epic OAuth exchange failed")


def authorization_code_for_token(code: str) -> dict:
    """Exchange an Epic **authorization code** for a bearer token.

    Secondary path used when a manual paste is an authorization code:
    ``grant_type=authorization_code``, ``code=<code>``, ``token_type=eg1``.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "token_type": "eg1",
    }
    return _token_request(data, "Epic OAuth failed")


def _token_request(data: dict, error_prefix: str) -> dict:
    response = requests.post(
        EPIC_TOKEN_URL,
        data=data,
        auth=(EPIC_CLIENT_ID, EPIC_CLIENT_SECRET),
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError as exc:  # noqa: BLE001
        raise EpicAuthError(f"{error_prefix} (non-JSON {response.status_code})") from exc
    if response.status_code >= 400:
        error = payload.get("errorCode") or payload.get("errorMessage") or "bad request"
        raise EpicAuthError(f"{error_prefix} ({response.status_code}): {error}")
    if not payload.get("access_token"):
        raise EpicAuthError("No access_token in token response")
    return payload


def obtain_token(code: str) -> dict:
    """Exchange a login code for a token, trying both Epic grant types.

    Epic's embedded flow hands over an exchange code (``grant_type=exchange_code``),
    but a manual paste may be an authorization code. Try the exchange-code grant
    first (what the bridge delivers), then the authorization-code grant.
    """
    try:
        return exchange_code_for_token(code)
    except EpicAuthError:
        return authorization_code_for_token(code)


def fetch_account_id(token: str) -> str:
    """Return the Epic account id for a token, or ``""`` if unknown."""
    try:
        response = requests.get(
            EPIC_ACCOUNT_URL,
            headers={"User-Agent": USER_AGENT, "Authorization": f"bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json().get("accountId") or "")
    except requests.RequestException:
        logger.warning("Could not resolve Epic account id", exc_info=True)
        return ""


def list_owned(token: str) -> list[dict]:
    """Return every owned game/asset via Epic's web services (no legendary).

    Mirrors Lutris' ``get_library``: reads the Library service for owned
    records, then resolves each record's metadata through the Catalog service.
    Returns a list of metadata dicts (with ``appName``/``title`` keys) suitable
    for :class:`~vitrine.sources.epic_source.EpicSource`.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Authorization": f"bearer {token}"})
    records = _library_records(session)
    items: list[dict] = []
    for record in records:
        namespace = record.get("namespace")
        catalog_item_id = record.get("catalogItemId")
        app_name = record.get("appName")
        if namespace == "ue" or not catalog_item_id or not app_name:
            continue
        details = _catalog_details(session, namespace, catalog_item_id)
        if not details:
            continue
        details.setdefault("appName", app_name)
        items.append(details)
    return items


def _library_records(session: requests.Session) -> list[dict]:
    records: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"includeMetadata": "true"}
        if cursor:
            params["cursor"] = cursor
        response = session.get(f"{EPIC_LIBRARY_URL}/library/api/public/items", params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload.get("records") or [])
        cursor = (payload.get("responseMetadata") or {}).get("nextCursor") or None
        if not cursor:
            break
    return records


def _catalog_details(session: requests.Session, namespace: str, catalog_item_id: str) -> dict:
    try:
        response = session.get(
            f"{EPIC_CATALOG_URL}/catalog/api/shared/namespace/{namespace}/bulk/items",
            params={
                "id": catalog_item_id,
                "includeDLCDetails": "true",
                "includeMainGameDetails": "true",
                "country": "US",
                "locale": "en",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get(catalog_item_id) or {}
    except requests.RequestException:
        logger.warning("Failed to resolve Epic catalog item %s", catalog_item_id, exc_info=True)
        return {}