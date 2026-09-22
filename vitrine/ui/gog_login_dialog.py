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
from urllib.parse import parse_qs, urlparse

import gi

logger = logging.getLogger(__name__)

gi.require_version("WebKit", "6.0")

from gi.repository import Adw, GLib, Gtk, WebKit  # noqa: E402

from ..sources.gog.auth import (  # noqa: E402
    AUTH_REDIRECT_URI,
    AUTH_URL,
    GOG_CLIENT_ID,
    GogCookieJar,
GogTokenStore,
    exchange_code_for_token,
)

#: How often the dialog re-checks the current address for the login redirect.
POLL_INTERVAL_MS = 300
#: Token-exchange attempts and the pause between them (the token endpoint can
#: be a moment behind the code after login).
TOKEN_FETCH_ATTEMPTS = 3
TOKEN_FETCH_RETRY_MS = 1200


class GogLoginDialog(Gtk.Window):
    """An embedded browser window for signing into GOG."""

    def __init__(
        self,
        store: GogTokenStore,
        on_complete: Callable[[bool, str | None], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Sign in to GOG")
        self.store = store
        self._on_complete = on_complete
        self._captured_user_id: str | None = None
        self._handled_code: str | None = None
        self.set_default_size(720, 860)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        self.webview = WebKit.WebView()
        self.webview.connect("load_changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)
        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        web_settings = WebKit.Settings()
        for prop in ("enable-media", "enable-mediasource", "enable-webaudio", "enable-webgl", "enable-media-stream"):
            setter = "set_" + prop
            if hasattr(web_settings, setter):
                getattr(web_settings, setter)(False)
        self.webview.set_settings(web_settings)
        self._poll_id: int | None = None
        self._capturing = False

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sign in to GOG", subtitle=""))
        header.set_show_end_title_buttons(True)
        reset_button = Gtk.Button(label="Reset session")
        reset_button.set_tooltip_text("Clear stored login, then start over")
        reset_button.add_css_class("destructive-action")
        reset_button.connect("clicked", self.reset_session)
        header.pack_start(reset_button)

        self._status = Gtk.Label(label="", wrap=True, xalign=0.0)
        self._status.add_css_class("dim-label")
        self._status.set_margin_top(6)
        self._status.set_margin_bottom(6)
        self._status.set_margin_start(12)
        self._status.set_margin_end(12)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(self._status)
        content.append(self.webview)

        self.set_titlebar(header)
        self.set_child(content)

        self.webview.load_uri(self._auth_url())
        self._start_polling()

    def _auth_url(self) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": GOG_CLIENT_ID,
            "redirect_uri": AUTH_REDIRECT_URI,
            "response_type": "code",
            "layout": "client2",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, _webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        if load_event == WebKit.LoadEvent.FINISHED:
            self._kick_poll()

    def _on_create_popup(
        self, _webview: WebKit.WebView, _navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        return None

    # -- code polling ---------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll_tick)

    def _kick_poll(self) -> None:
        self._poll_tick()

    def _stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll_tick(self) -> bool:
        if not self._capturing:
            self._check_redirect()
        return True

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status.set_text(("Error: " if error else "") + message)
        self._status.set_visible(bool(message))

    # -- session reset --------------------------------------------------------

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        self.store.clear()
        self._capturing = False
        self._stop_polling()
        self._set_status("")
        self.webview.load_uri(self._auth_url())
        self._start_polling()

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
                self._finish(True)
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

    def _finish(self, ok: bool) -> None:
        self._stop_polling()
        self._capturing = True
        if self._on_complete is not None:
            self._on_complete(ok, getattr(self, "_captured_user_id", None))
        self.close()


def _extract_code(address: str) -> str:
    """Pull the ``code`` query parameter from the login redirect URL."""
    parsed = urlparse(address)
    query = parse_qs(parsed.query)
    code = query.get("code", [""])[0]
    return code.strip() or ""