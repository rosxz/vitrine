# ruff: noqa: E402

"""Steam login window (embedded WebKit).

Signs the user in inside the app using a real WebKitWebView (the GTK4 build,
``webkitgtk_6_0``). Login cookies are captured from the live cookie manager
when the load lands on Steam's ``/about`` redirect URI (session cookies never
reach WebKit's persistent file, so the manager is the only reliable source);
the short-lived ``webapi_token`` is fetched with those cookies and both are
saved to the account's durable token store.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

logger = logging.getLogger(__name__)

gi.require_version("WebKit", "6.0")

from gi.repository import Adw, Gio, GLib, Gtk, WebKit  # noqa: E402

from ..sources.steam.auth import (
    CookieJar,
    SteamAuthError,
    SteamTokenStore,
)

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
        # Netscape-text file so WebKit flushes cookie state; session cookies
        # (e.g. ``sessionid``) are only ever alive in the manager itself, so
        # capture always reads from the live manager, never from this file.
        self.store.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        session = WebKit.NetworkSession.get_default()
        self._cookie_manager = session.get_cookie_manager()
        self._cookie_manager.set_persistent_storage(
            str(self.store.cookie_file),
            WebKit.CookiePersistentStorage.TEXT,
        )

        self.webview = WebKit.WebView()
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sign in to Steam", subtitle=""))
        header.set_show_end_title_buttons(True)
        reset_button = Gtk.Button(label="Reset session")
        reset_button.set_tooltip_text("Clear stored login and cookies, then start over")
        reset_button.add_css_class("destructive-action")
        reset_button.connect("clicked", self.reset_session)
        header.pack_start(reset_button)

        self.set_titlebar(header)
        self.set_child(self.webview)

        self.webview.load_uri(LOGIN_URL)

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, _webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        if load_event == WebKit.LoadEvent.FINISHED:
            url = self.webview.get_uri() or ""
            # Steam may land on /about directly, or a QR login flow may carry a
            # query/hash; either way, once we are on the store post-login path
            # we try to capture, retrying briefly for late-appearing cookies.
            if "steamLoginSecure" not in _cached_names(self.store) and self._is_after_login(url):
                self._attempt_capture(retries=3)

    def _on_create_popup(
        self, _webview: WebKit.WebView, _navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        # Return None so the target opens in this same view, keeping the cookie
        # session intact and avoiding ownership/GC issues from swapping children.
        return None

    # -- session reset ----------------------------------------------------------

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        """Clear stored credentials and the browser's cookies, then reload."""
        self.store.clear()
        if self.store.cookie_file.exists():
            try:
                self.store.cookie_file.unlink()
            except OSError:
                pass
        try:
            data_manager = WebKit.NetworkSession.get_default().get_website_data_manager()
            data_manager.clear(
                WebKit.WebsiteDataTypes.COOKIES,
                0,
                None,
                self._on_cookies_cleared,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to clear cookie manager: %s", exc)
            self._on_cookies_cleared(None, None)

    def _on_cookies_cleared(self, _manager, _result) -> None:
        self.webview.load_uri(LOGIN_URL)

    # -- credential capture ---------------------------------------------------

    def _is_after_login(self, url: str) -> bool:
        """True if ``url`` looks like Steam's signed-in store destination.

        Compares the URL *path* (not the query string, which can carry
        ``redir=/about`` on the login page itself).
        """
        from urllib.parse import urlparse

        if url.startswith(REDIRECT_URI):
            return True
        if not url.startswith("https://store.steampowered.com/"):
            return False
        path = urlparse(url).path
        return path == "/about" or path == "/account" or path.startswith("/about") or path.startswith("/account")

    def _attempt_capture(self, retries: int = 0) -> None:
        """Read the live cookie manager; session cookies never hit the disk
        file, so the manager is the only reliable source."""
        if retries > 0:

            def delayed() -> bool:
                self._read_cookies(retries)
                return False

            GLib.timeout_add(500, delayed)
            return
        self._read_cookies(retries)

    def _read_cookies(self, retries: int) -> None:
        self._cookie_manager.get_all_cookies(None, lambda mgr, res: self._on_cookies_read(mgr, res, retries))

    def _on_cookies_read(
        self, _manager: WebKit.CookieManager, result: Gio.AsyncResult, retries: int = 0
    ) -> None:
        try:
            cookies = cast_cookie_list(self._cookie_manager.get_all_cookies_finish(result))
            missing = not cookies.get("steamLoginSecure") or not cookies.get("sessionid")
            if missing and retries > 0:
                # Cookies can arrive a moment after the redirect finishes.
                self._attempt_capture(retries - 1)
                return
            if missing:
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


def cast_cookie_list(cookies) -> CookieJar:
    """Convert the ``Soup.Cookie`` list from the cookie manager into a
    :class:`CookieJar`, keeping session cookies (no expires) too."""
    jar = CookieJar()
    for cookie in cookies or []:
        jar.add(
            {
                "name": cookie.get_name(),
                "value": cookie.get_value(),
                "domain": cookie.get_domain(),
                "path": cookie.get_path(),
                "secure": bool(cookie.get_secure()),
                "http_only": bool(cookie.get_http_only()),
                "expires": _cookie_expiry(cookie),
            }
        )
    return jar


def _cookie_expiry(cookie) -> int | None:
    expires = cookie.get_expires()
    if expires is None:
        return None
    # GLib.DateTime -> unix seconds (0 == session/not-set).
    try:
        stamp = expires.to_unix()
    except (AttributeError, TypeError, ValueError):
        return None
    return int(stamp) if stamp else None


def _cached_names(store: SteamTokenStore) -> set[str]:
    """Names of cookies already stored for this account."""
    return {c["name"] for c in store.cookies().to_dict()}