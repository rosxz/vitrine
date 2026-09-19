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
import time
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
#: Token-exchange attempts and the pause between them (the token endpoint can
#: be a moment behind the session cookies after a QR login).
TOKEN_FETCH_ATTEMPTS = 3
TOKEN_FETCH_RETRY_MS = 1200


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
        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        # The Steam login flow needs nothing from WebKit's media/GStreamer stack;
        # disabling it avoids the web process spamming GStreamer GPU criticals.
        web_settings = WebKit.Settings()
        for prop in ("enable-media", "enable-mediasource", "enable-webaudio", "enable-webgl", "enable-media-stream"):
            setter = "set_" + prop
            if hasattr(web_settings, setter):
                getattr(web_settings, setter)(False)
        self.webview.set_settings(web_settings)
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

        # A slim banner under the header for progress/errors; the window stays
        # open on failure so the reset button is always reachable.
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

    def _stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _poll_tick(self) -> bool:
        """Keep polling (return True) while the dialog is alive; read cookies."""
        if not self._capturing:
            self._read_cookies()
        return True

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status.set_text(("Error: " if error else "") + message)
        self._status.set_visible(bool(message))

    def _read_cookies(self) -> None:
        self._cookie_manager.get_all_cookies(None, self._on_cookies_read)

    # -- session reset ----------------------------------------------------------

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        """Clear stored credentials and the browser's cookies, then reload."""
        self.store.clear()
        self._capturing = False
        self._stop_polling()
        if self.store.cookie_file.exists():
            try:
                self.store.cookie_file.unlink()
            except OSError:
                pass
        # Clear WebKit's cookie store, then eagerly drop every cookie from the
        # live manager too (clear() alone can leave session cookies behind).
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
        try:
            self._cookie_manager.get_all_cookies(None, self._drop_all_cookies)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to enumerate cookies after reset: %s", exc)
            self._after_cookies_dropped()

    def _drop_all_cookies(self, _manager: WebKit.CookieManager, result: Gio.AsyncResult) -> None:
        try:
            cookies = self._cookie_manager.get_all_cookies_finish(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to list cookies after reset: %s", exc)
            cookies = []
        for cookie in cookies or []:
            try:
                self._cookie_manager.delete_cookie(cookie, None, lambda *_: None)
            except Exception:  # noqa: BLE001
                pass
        self._after_cookies_dropped()

    def _after_cookies_dropped(self) -> None:
        self._capturing = False
        self._set_status("")
        self.webview.load_uri(LOGIN_URL)
        self._start_polling()

    # -- credential capture ---------------------------------------------------

    def _on_cookies_read(
        self, _manager: WebKit.CookieManager, result: Gio.AsyncResult
    ) -> None:
        try:
            cookies = cast_cookie_list(self._cookie_manager.get_all_cookies_finish(result))
        except Exception as exc:  # noqa: BLE001 - cookie read failed; keep polling
            logger.warning("Cookie read failed: %s", exc)
            return  # the repeating poller keeps going
        if cookies.get("steamLoginSecure") and cookies.get("sessionid"):
            # Session cookies appeared (QR completed). Stop polling and validate.
            self._stop_polling()
            self._capturing = True
            self._set_status("Steam detected your login — completing…")
            self._save_and_finish(cookies)
        # else: keep polling (the repeating timeout stays live)

    def _save_and_finish(self, cookies: CookieJar) -> None:
        # The token endpoint can lag a moment behind the session cookies, so
        # retry a couple of times before showing a failure.
        last_error: Exception | None = None
        for attempt in range(TOKEN_FETCH_ATTEMPTS):
            try:
                self.store.set_credentials(cookies)
                token = self.store.fetch_access_token()
                if token:
                    self._set_status("")
                    self._finish(True)
                    return
                last_error = SteamAuthError("Login succeeded but no access token was returned")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt + 1 < TOKEN_FETCH_ATTEMPTS:
                # Hold the GLib loop briefly, then retry with fresh timing.
                deadline = time.monotonic() + TOKEN_FETCH_RETRY_MS / 1000
                while time.monotonic() < deadline:
                    while GLib.main_context_default().pending():
                        GLib.main_context_default().iteration(False)

        # Exhausted retries: keep the window open with a clear message so the
        # user can reset and retry.
        logger.warning("Steam token fetch failed after %d attempts: %s", TOKEN_FETCH_ATTEMPTS, last_error)
        self._set_status(f"Steam validation failed: {last_error}", error=True)
        self._capturing = False
        self._start_polling()

    def _finish(self, ok: bool) -> None:
        self._stop_polling()
        self._capturing = True
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