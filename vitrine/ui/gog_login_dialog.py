# ruff: noqa: E402

"""GOG login window (embedded WebKit).

Mirrors the Steam login window: the user signs in inside the app with a real
WebKitWebView (the GTK4 build, ``webkitgtk_6_0``). GOG uses an OAuth2 code
flow, so the dialog watches the WebView's address for the
``embed.gog.com/on_login_success?code=...`` redirect, extracts the one-time
``code``, exchanges it for a bearer token and persists it to the account's
durable token store.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import gi

logger = logging.getLogger(__name__)

gi.require_version("WebKit", "6.0")

from gi.repository import GLib, Gtk  # noqa: E402

from vitrine.sources.gog.auth import (  # noqa: E402
    AUTH_REDIRECT_URI,
    AUTH_URL,
    GOG_CLIENT_ID,
    GogCookieJar,
    GogTokenStore,
    exchange_code_for_token,
)
from vitrine.ui.login_base import WebKitLoginDialog

#: How often the dialog re-checks the current address for the login redirect.
POLL_INTERVAL_MS = 300
#: Token-exchange attempts and the pause between them (the token endpoint can
#: be a moment behind the code after login).
TOKEN_FETCH_ATTEMPTS = 3
TOKEN_FETCH_RETRY_MS = 1200


class GogLoginDialog(WebKitLoginDialog):
    """An embedded browser window for signing into GOG."""

    dialog_title = "Sign in to GOG"

    def __init__(
        self,
        store: GogTokenStore,
        on_complete: Callable[[bool, str | None], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        self.store = store
        self._captured_user_id: str | None = None
        self._handled_code: str | None = None
        super().__init__(on_complete=on_complete, parent=parent)

    def poll_interval_ms(self) -> int:
        return POLL_INTERVAL_MS

    def _auth_url(self) -> str:
        params = {
            "client_id": GOG_CLIENT_ID,
            "redirect_uri": AUTH_REDIRECT_URI,
            "response_type": "code",
            "layout": "client2",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _poll(self) -> None:
        self._check_redirect()

    # -- credential capture ---------------------------------------------------

    def _check_redirect(self) -> None:
        address = self.webview.get_uri() or ""
        if "on_login_success" not in address:
            return
        code = _extract_code(address)
        if not code or code == self._handled_code:
            return
        # Found the one-time code: stop polling and exchange it for a token.
        self._handled_code = code
        self._stop_polling()
        self._capturing = True
        self._set_status("GOG detected your login — completing…")
        self._exchange_code(code)

    def _exchange_code(self, code: str) -> None:
        last_error: Exception | None = None
        for attempt in range(TOKEN_FETCH_ATTEMPTS):
            try:
                token_data = exchange_code_for_token(code)
                user_id = str(token_data.get("user_id") or self.store.user_id or "0")
                # Persist credentials under the *real* account id, not the
                # placeholder store that was constructed before login revealed
                # who signed in.
                real_store = type(self.store)(self.store.secret_dir, user_id)
                real_store.set_credentials(
                    GogCookieJar([]),
                    access_token=token_data.get("access_token", ""),
                    refresh_token=token_data.get("refresh_token", ""),
                    expires_in=int(token_data.get("expires_in") or 0),
                )
                self.store = real_store
                self._set_status("")
                self._captured_user_id = user_id
                self.finish(True, user_id)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt + 1 < TOKEN_FETCH_ATTEMPTS:
                deadline = time.monotonic() + TOKEN_FETCH_RETRY_MS / 1000
                while time.monotonic() < deadline:
                    while GLib.main_context_default().pending():
                        GLib.main_context_default().iteration(False)

        logger.warning("GOG token exchange failed after %d attempts: %s", TOKEN_FETCH_ATTEMPTS, last_error)
        self._set_status(f"GOG validation failed: {last_error}", error=True)
        # Leave the failure page so polling does not keep re-reading the same
        # code off the white on_login_success screen; the user can reset/retry.
        self._capturing = False
        self._start_polling()

    # -- session reset ----------------------------------------------------------

    def _on_reset(self) -> None:
        self.store.clear()


def _extract_code(address: str) -> str:
    """Pull the ``code`` query parameter from the login redirect URL."""
    parsed = urlparse(address)
    query = parse_qs(parsed.query)
    code = query.get("code", [""])[0]
    return code.strip() or ""