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

from gi.repository import Gio, GLib, Gtk, WebKit  # noqa: E402

from ..sources.steam.auth import CookieJar, SteamAuthError, SteamTokenStore
from .login_base import WebKitLoginDialog

LOGIN_URL = "https://store.steampowered.com/login/?redir=/about"

#: How often the dialog polls the cookie manager for the session cookies.
POLL_INTERVAL_MS = 800
#: Token-exchange attempts and the pause between them (the token endpoint can
#: be a moment behind the session cookies after a QR login).
TOKEN_FETCH_ATTEMPTS = 3
TOKEN_FETCH_RETRY_MS = 1200


class SteamLoginDialog(WebKitLoginDialog):
    """An embedded browser window for signing into Steam."""

    dialog_title = "Sign in to Steam"
    login_url = LOGIN_URL

    def __init__(
        self,
        store: SteamTokenStore,
        on_complete: Callable[[bool], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        self.store = store
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
        super().__init__(on_complete=on_complete, parent=parent)

    def poll_interval_ms(self) -> int:
        return POLL_INTERVAL_MS

    def _poll(self) -> None:
        self._read_cookies()

    def _read_cookies(self) -> None:
        self._cookie_manager.get_all_cookies(None, self._on_cookies_read)

    # -- credential capture ---------------------------------------------------

    def _on_cookies_read(self, _manager: WebKit.CookieManager, result: Gio.AsyncResult) -> None:
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
                    self._finish_capture(True)
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

    def _finish_capture(self, ok: bool) -> None:
        self.finish(ok)

    # -- session reset ----------------------------------------------------------

    def _on_reset(self) -> None:
        """Clear Steam's durable store, the cookie file, and WebKit's cookies."""
        self.store.clear()
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
        self._set_status("")


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