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

#: How often the dialog polls the cookie manager for the session cookies.
POLL_INTERVAL_MS = 800


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
        self.webview.connect("load_changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)
        self._poll_id: int | None = None
        self._capturing = False

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
        self._start_polling()

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, _webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        # A page load is a convenient kick for the cookie poller, but the
        # poller is the real detector (QR login may not navigate to /about).
        if load_event == WebKit.LoadEvent.FINISHED:
            self._kick_poll()

    def _on_create_popup(
        self, _webview: WebKit.WebView, _navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        # Return None so the target opens in this same view, keeping the cookie
        # session intact and avoiding ownership/GC issues from swapping children.
        return None

    # -- cookie polling --------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll_tick)

    def _kick_poll(self) -> None:
        self._poll_tick()

    def _poll_tick(self) -> bool:
        """Read cookies once; the callback re-arms the poller unless the
        session is complete. Return True to keep the timeout, False to stop."""
        if self._capturing:
            return False
        if _cached_names(self.store) & {"steamLoginSecure", "sessionid"} == {"steamLoginSecure", "sessionid"}:
            # Already fully captured on an earlier run of this dialog.
            self._capturing = True
            GLib.idle_add(lambda: self._finish(True))
            return False
        self._read_cookies()
        return False  # the callback re-arms polling

    def _read_cookies(self) -> None:
        self._cookie_manager.get_all_cookies(None, self._on_cookies_read)

    def _reschedule(self) -> None:
        if not self._capturing:
            self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll_tick)

    # -- session reset ----------------------------------------------------------

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        """Clear stored credentials and the browser's cookies, then reload."""
        self.store.clear()
        self._capturing = False
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
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
        self._capturing = False
        self.webview.load_uri(LOGIN_URL)
        self._start_polling()

    # -- credential capture ---------------------------------------------------

    def _on_cookies_read(
        self, _manager: WebKit.CookieManager, result: Gio.AsyncResult
    ) -> None:
        try:
            cookies = cast_cookie_list(self._cookie_manager.get_all_cookies_finish(result))
            if cookies.get("steamLoginSecure") and cookies.get("sessionid"):
                self._capturing = True
                self._save_and_finish(cookies)
                return
        except Exception as exc:  # noqa: BLE001 - cookie read failed; keep polling
            logger.warning("Cookie read failed: %s", exc)
        self._reschedule()

    def _save_and_finish(self, cookies: CookieJar) -> None:
        try:
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