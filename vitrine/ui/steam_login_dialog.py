"""Steam login window.

Steam's web login cannot be embedded with WebKit2 while GTK4 is loaded in the
same process (the 4.1 typelib still declares a Gtk-3.0 dependency, which
PyGObject rejects once GTK4 is loaded). Vitrine therefore opens the Steam store
login in the user's default browser and asks for the resulting session cookie.

The ``steamLoginSecure`` cookie is the piece that proves a store session; the
``sessionid`` cookie is needed alongside it by some endpoints. Both, plus any
others copied, are stored in the account's durable token cache (session cookies
included), then the short-lived ``webapi_token`` is fetched with them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Gio, Gtk

from ..sources.steam.auth import CookieJar, SteamAuthError, SteamTokenStore

logger = logging.getLogger(__name__)

LOGIN_URL = "https://store.steampowered.com/login/?redir=/about"

HELP_TEXT = (
    "Sign in to Steam in your browser, then paste the session cookies below.\n\n"
    "How to get them:\n"
    "1. Click “Open Steam login” and sign in.\n"
    "2. Open your browser's developer tools (F12) → Application/Storage → Cookies\n"
    "   for store.steampowered.com.\n"
    "3. Copy the whole cookie list and paste it here.\n\n"
    "The session stays saved until it expires; Vitrine refreshes its access "
    "token silently in the meantime."
)


class SteamLoginDialog(Gtk.Window):
    """A small window to connect Vitrine to a Steam account."""

    def __init__(
        self,
        store: SteamTokenStore,
        on_complete: Callable[[bool], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Connect Steam")
        self.store = store
        self._on_complete = on_complete
        self.set_default_size(560, 560)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        help_label = Gtk.Label(label=HELP_TEXT, wrap=True, justify=Gtk.Justification.LEFT)
        help_label.set_margin_top(12)
        help_label.set_margin_bottom(12)
        help_label.set_margin_start(16)
        help_label.set_margin_end(16)

        self._cookies = Gtk.TextView()
        self._cookies.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._cookies.set_placeholder_text(
            "sessionid=...\nsteamLoginSecure=...\n# paste the Netscape-format cookie list"
        )
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._cookies)
        scroller.set_vexpand(True)
        scroller.set_margin_start(16)
        scroller.set_margin_end(16)

        open_button = Gtk.Button(label="Open Steam login")
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", self._on_open_login)

        connect_button = Gtk.Button(label="Connect")
        connect_button.add_css_class("suggested-action")
        connect_button.connect("clicked", self._on_connect)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _b: self.close())

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(12)
        actions.set_margin_bottom(16)
        actions.set_margin_start(16)
        actions.set_margin_end(16)
        actions.append(cancel_button)
        spacer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        spacer.set_hexpand(True)
        actions.append(spacer)
        actions.append(open_button)
        actions.append(connect_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(help_label)
        content.append(scroller)
        content.append(actions)

        self.set_child(content)

    # -- behaviour ------------------------------------------------------------

    def _on_open_login(self, _button: Gtk.Button) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(LOGIN_URL)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not open browser for Steam login: %s", error)

    def _on_connect(self, _button: Gtk.Button) -> None:
        buffer = self._cookies.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        jar = _parse_cookie_dump(text)
        if not jar.cookies:
            buffer2 = self._cookies.get_buffer()
            buffer2.set_text("No cookies found. Paste the cookie list first.", -1)
            return
        try:
            self.store.set_credentials(jar)
            token = self.store.fetch_access_token()
            if not token:
                raise SteamAuthError("Sign-in succeeded but no access token was returned")
        except SteamAuthError as error:
            logger.warning("Steam connect failed: %s", error)
            buffer2 = self._cookies.get_buffer()
            buffer2.set_text(f"Connection failed: {error}", -1)
            return
        self._finish(True)

    def _finish(self, ok: bool) -> None:
        if self._on_complete is not None:
            self._on_complete(ok)
        if ok:
            self.close()


def _parse_cookie_dump(text: str) -> CookieJar:
    """Parse user-pasted cookies.

    Accepts the Netscape-format lines produced by browser cookie exporters
    (``domain\t...\tname\tvalue``) and Chrome's ``Name=Value`` rows.
    """
    jar = CookieJar()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            fields = line.split("\t")
            if len(fields) >= 7:
                domain, _flag, path, secure_flag, expires, name, value = fields[:7]
                try:
                    expires_value = int(expires) if expires else None
                except ValueError:
                    expires_value = None
                jar.add(
                    {
                        "domain": domain,
                        "path": path,
                        "secure": secure_flag.lower() == "true",
                        "expires": expires_value,
                        "name": name,
                        "value": value,
                    }
                )
                continue
        # Name=Value rows (Chrome devtools).
        if "=" in line and not line.startswith("="):
            name, value = line.split("=", 1)
            jar.add({"name": name.strip(), "value": value.strip()})
    return jar