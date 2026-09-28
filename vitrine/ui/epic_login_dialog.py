# ruff: noqa: E402

"""Epic Games login window (embedded WebKit).

Mirrors the Steam/GOG login windows: the user signs in inside the app with a
real WebKitWebView (the GTK4 build, ``webkitgtk_6_0``). The flow follows the
Lutris EGS approach -- we load ``/id/login?redirectUrl=<api/redirect>``; after
the user signs in, Epic redirects to ``/id/api/redirect`` whose response body is
the JSON ``{"authorizationCode": "..."}``. We read that body, exchange the
authorization code for a bearer token, and let legendary import it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

logger = logging.getLogger(__name__)

gi.require_version("WebKit", "6.0")

from gi.repository import Gtk, WebKit  # noqa: E402

from ..sources.epic import legendary as lg  # noqa: E402
from ..sources.epic.auth import (  # noqa: E402
    EPIC_AUTH_URL,
    EPIC_REDIRECT_PREFIX,
    EpicTokenStore,
    obtain_token,
)
from .login_base import WebKitLoginDialog


class EpicLoginDialog(WebKitLoginDialog):
    """An embedded browser window for signing into Epic Games."""

    dialog_title = "Sign in to Epic Games"
    login_url = EPIC_AUTH_URL

    def __init__(
        self,
        store: EpicTokenStore,
        on_complete: Callable[[bool, str | None, str], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        self.store = store
        self._captured_account_id: str | None = None
        self._handled_code: str | None = None
        self._handling = False
        super().__init__(on_complete=on_complete, parent=parent)

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        if load_event != WebKit.LoadEvent.FINISHED:
            return
        url = webview.get_uri() or ""
        if self._handling:
            return
        # Lutris approach: the login completes when Epic redirects to the
        # /id/api/redirect URI, whose body is the authorization code JSON.
        if url.startswith(EPIC_REDIRECT_PREFIX):
            self._handling = True
            self._set_status("Epic detected your login — completing…")
            resource = webview.get_main_resource()
            try:
                resource.get_data(None, self._on_resource_data, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read Epic login response: %s", exc)
                self._set_status(f"Epic validation failed: {exc}", error=True)
                self._handling = False

    def _on_resource_data(self, resource: WebKit.WebResource, result, user_data) -> None:
        """Async callback: the redirect page body holds the authorization code."""
        data = None
        try:
            data = resource.get_data_finish(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not finish reading Epic login response: %s", exc)
        text = str(data or "")
        code = _extract_code_from_body(text)
        if code and code != self._handled_code:
            self._handled_code = code
            self._exchange_code(code)
        else:
            self._set_status("Epic sign-in incomplete — no authorization code", error=True)
            self._handling = False

    # -- credential exchange --------------------------------------------------

    def _exchange_code(self, code: str) -> None:
        last_error: Exception | None = None

        # Path 1: let legendary consume the code FIRST and write its own
        # user.json. The code is single-use, so it must not be spent by our own
        # exchange before legendary has a chance to persist its session.
        legendary_ok = False
        if lg.is_installed():
            try:
                lg.auth(code)  # `legendary auth --code` -> writes user.json
                legendary_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("legendary auth failed: %s", exc)
                last_error = exc
        else:
            logger.info("legendary not installed; skipping legendary import")

        # Path 2: our own HTTP exchange, used for account-id resolution and when
        # legendary is not installed. If legendary already spent the (single-use)
        # code in Path 1 this may fail -- that is fine, legendary is authoritative.
        token: dict | None = None
        account_id = ""
        try:
            token = obtain_token(code)
            account_id = self._resolve_account(token)
            self.store.set_credentials(code, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vitrine HTTP exchange failed (non-fatal if legendary worked): %s", exc)
            if last_error is None:
                last_error = exc

        # Resolve the account id from legendary's saved session or our store.
        if not account_id:
            account_id = self._resolve_account(self.store.load().get("token") or {})
        if not account_id and lg.is_installed():
            account_id = str(lg.read_credentials().get("account_id") or "")

        if token is not None or legendary_ok:
            self._set_status("")
            self._captured_account_id = account_id or self.store.account_id
            self.finish(True, self._captured_account_id, code)
            return

        logger.warning("Epic auth failed: %s", last_error)
        self._set_status(f"Epic validation failed: {last_error}", error=True)
        self._handling = False

    def _resolve_account(self, token: dict) -> str:
        """Best-effort account id, preferring the id embedded in the token."""
        for key in ("account_id", "accountId", "sub", "sub_entity_id"):
            value = token.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    # -- session reset ----------------------------------------------------------

    def _on_reset(self) -> None:
        self.store.clear()
        self._handled_code = None
        self._handling = False


def _extract_code_from_body(text: str) -> str:
    """Pull ``authorizationCode`` (or ``exchangeCode``/``code``) from JSON text."""
    import re

    for key in ("authorizationCode", "exchangeCode", "code"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1).strip()
    return ""