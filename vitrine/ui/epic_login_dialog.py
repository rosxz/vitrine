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

from gi.repository import Adw, Gtk, WebKit  # noqa: E402

from ..sources.epic import legendary as lg  # noqa: E402
from ..sources.epic.auth import (  # noqa: E402
    EPIC_AUTH_URL,
    EPIC_REDIRECT_PREFIX,
    EpicTokenStore,
    obtain_token,
)


class EpicLoginDialog(Gtk.Window):
    """An embedded browser window for signing into Epic Games."""

    def __init__(
        self,
        store: EpicTokenStore,
        on_complete: Callable[[bool, str | None, str], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Sign in to Epic Games")
        self.store = store
        self._on_complete = on_complete
        self._captured_account_id: str | None = None
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

        self._handling = False

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sign in to Epic Games", subtitle=""))
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

        self.webview.load_uri(EPIC_AUTH_URL)

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

    def _on_create_popup(
        self, _webview: WebKit.WebView, _navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        return None

    # -- helpers --------------------------------------------------------------

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status.set_text(("Error: " if error else "") + message)
        self._status.set_visible(bool(message))

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        self.store.clear()
        self._handling = False
        try:
            data_manager = WebKit.NetworkSession.get_default().get_website_data_manager()
            data_manager.clear(
                WebKit.WebsiteDataTypes.COOKIES,
                0,
                None,
                self._on_cookies_cleared,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to clear Epic cookies: %s", exc)
            self._after_reset()

    def _on_cookies_cleared(self, _manager, _result) -> None:
        self._after_reset()

    def _after_reset(self) -> None:
        self._set_status("")
        self._handled_code = None
        self._handling = False
        self.webview.load_uri(EPIC_AUTH_URL)

    # -- credential exchange --------------------------------------------------

    def _exchange_code(self, code: str) -> None:
        last_error: Exception | None = None

        # Path 1: our own HTTP exchange -- works even without legendary installed.
        token: dict | None = None
        account_id = ""
        try:
            token = obtain_token(code)
            account_id = self._resolve_account(token)
            self.store.set_credentials(code, token)
            if lg.is_installed():
                # Write the token into legendary's own session file so it can
                # authenticate (and refresh) without us re-using the spent code.
                try:
                    lg.set_credentials(token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not write legendary credentials: %s", exc)
        except Exception as exc:  # noqa: BLE001 - fall back to legendary
            logger.warning("Vitrine HTTP exchange failed: %s", exc)
            last_error = exc

        # Path 2: if our own exchange did not yield a token, let legendary
        # import the login code itself.
        legendary_ok = bool(token)
        if not legendary_ok:
            try:
                if lg.is_installed():
                    lg.auth(code)
                    legendary_ok = True
                else:
                    logger.info("legendary not installed; skipping legendary import")
                    if last_error is None:
                        last_error = lg.LegendaryError("legendary is not installed")
            except Exception as exc:  # noqa: BLE001
                logger.warning("legendary auth failed: %s", exc)
                if last_error is None:
                    last_error = exc

        # Resolve the account id from the stored token if we got none above.
        if not account_id:
            account_id = self._resolve_account(self.store.load().get("token") or {})

        if token is not None or legendary_ok:
            self._set_status("")
            self._captured_account_id = account_id or self.store.account_id
            self._finish(True, code)
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

    def _finish(self, ok: bool, code: str = "") -> None:
        self._handling = True
        if self._on_complete is not None:
            self._on_complete(ok, self._captured_account_id, code)
        self.close()


def _extract_code_from_body(text: str) -> str:
    """Pull ``authorizationCode`` (or ``exchangeCode``/``code``) from JSON text."""
    import re

    for key in ("authorizationCode", "exchangeCode", "code"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1).strip()
    return ""

    def _exchange_code(self, code: str) -> None:
        last_error: Exception | None = None

        # Primary path: let legendary (the battle-tested backend) import the
        # exchange code. This is what actually grants install/launch rights.
        legendary_ok = False
        try:
            from ..sources.epic import legendary as lg

            lg.auth(code)
            legendary_ok = True
        except Exception as exc:  # noqa: BLE001 - legendary may be missing
            logger.warning("legendary auth failed: %s", exc)
            last_error = exc

        # Secondary path: our own HTTP exchange, mainly to resolve account_id.
        account_id = ""
        if legendary_ok:
            try:
                token = obtain_token(code)
                account_id = self._resolve_account(token)
                self.store.set_credentials(code, token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vitrine HTTP exchange failed (non-fatal if legendary worked): %s", exc)
                if not account_id:
                    account_id = self._resolve_account(self.store.load().get("token") or {})

        if legendary_ok:
            self._set_status("")
            self._captured_account_id = account_id or self.store.account_id
            self._finish(True, code)
            return

        # Neither path worked: resume polling so a fresh code can arrive.
        logger.warning("Epic auth failed: %s", last_error)
        self._set_status(f"Epic validation failed: {last_error} — awaiting a fresh code", error=True)
        self._handled_code = None
        self._capturing = False
        self._start_polling()

    def _resolve_account(self, token: dict) -> str:
        """Best-effort account id, preferring the id embedded in the token."""
        for key in ("account_id", "accountId", "sub", "sub_entity_id"):
            value = token.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _finish(self, ok: bool, code: str = "") -> None:
        self._stop_polling()
        self._capturing = True
        if self._on_complete is not None:
            self._on_complete(ok, self._captured_account_id, code)
        self.close()


