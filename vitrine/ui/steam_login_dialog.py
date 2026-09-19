"""Steam login window (embedded WebKit).

Signs the user in inside the app using a real WebKitWebView (the GTK4 build,
``webkitgtk_6_0``). Login cookies are captured by pointing the default
network session's cookie manager at a persistent Netscape-text file; when the
load lands on Steam's ``/about`` redirect URI the cookie file is read into a
:class:`CookieJar`, the short-lived ``webapi_token`` is fetched with those
cookies, and both are saved to the account's durable token store.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

from ..sources.steam.auth import (
    SteamAuthError,
    SteamTokenStore,
    read_netscape_cookies,
)

logger = logging.getLogger(__name__)

gi.require_version("WebKit", "6.0")

from gi.repository import Adw, Gtk, WebKit  # noqa: E402

LOGIN_URL = "https://store.steampowered.com/login/?redir=/about"
REDIRECT_URI = "https://store.steampowered.com/about"


class SteamLoginDialog(Gtk.Window):
    """An embedded browser window for signing into Steam."""

    def __init__(
        self,
        store: SteamTokenStore,
        on_complete: Callable[[bool], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Sign in to Steam")
        self.store = store
        self._on_complete = on_complete
        self.set_default_size(720, 860)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        # Point the shared network session's cookie manager at a persistent
        # Netscape-text file so login cookies are captured on disk.
        self.store.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        if self.store.cookie_file.exists():
            self.store.cookie_file.unlink()
        session = WebKit.NetworkSession.get_default()
        cookie_manager = session.get_cookie_manager()
        cookie_manager.set_persistent_storage(
            str(self.store.cookie_file),
            WebKit.CookiePersistentStorage.TEXT,
        )

        self.webview = WebKit.WebView()
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sign in to Steam", subtitle=""))
        header.set_show_end_title_buttons(True)

        self.set_titlebar(header)
        self.set_child(self.webview)

        self.webview.load_uri(LOGIN_URL)

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, _webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        if load_event == WebKit.LoadEvent.FINISHED:
            url = self.webview.get_uri() or ""
            if url.startswith(REDIRECT_URI):
                self._capture_credentials()

    def _on_create_popup(
        self, _webview: WebKit.WebView, navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        uri = navigation.get_request().get_uri()
        # Steam's 2FA and redirect flows can open popups; load them in this
        # same window so the cookie session stays intact.
        self.webview = WebKit.WebView()
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)
        self.set_child(self.webview)
        self.webview.load_uri(uri)
        return self.webview

    # -- credential capture ---------------------------------------------------

    def _capture_credentials(self) -> None:
        try:
            cookies = read_netscape_cookies(self.store.cookie_file)
            if not cookies.get("steamLoginSecure") or not cookies.get("sessionid"):
                raise SteamAuthError("Login did not produce Steam session cookies")
            self.store.set_credentials(cookies)
            token = self.store.fetch_access_token()
            if not token:
                raise SteamAuthError("Login succeeded but no access token was returned")
        except Exception as exc:  # noqa: BLE001 - surface any login failure
            logger.exception("Steam login capture failed: %s", exc)
            self._finish(False)
            return
        self._finish(True)

    def _finish(self, ok: bool) -> None:
        if self._on_complete is not None:
            self._on_complete(ok)
        self.close()