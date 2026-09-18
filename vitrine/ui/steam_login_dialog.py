"""Steam login window.

Vitrine cannot embed a real Steam login webview: WebKit2's 4.1 typelib still
declares a Gtk-3.0 dependency, which PyGObject rejects once GTK4 is loaded in
the same process. Sign-in therefore happens in the user's own browser, and this
window collects the resulting session cookies ("web window" flow):

1. The user clicks "Open Steam login" (launches the default browser).
2. They sign in and copy the ``store.steampowered.com`` cookies.
3. They paste them here and click Connect.

The ``steamLoginSecure`` cookie proves the session; ``sessionid`` is needed by
some endpoints. All cookies (session cookies included) are stored in the
account's durable token cache, then the short-lived ``webapi_token`` is
fetched with them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Gio, Gtk

from ..sources.steam.auth import CookieJar, SteamAuthError, SteamTokenStore

logger = logging.getLogger(__name__)

LOGIN_URL = "https://store.steampowered.com/login/?redir=/about"

#: Two-column instructions shown above the paste area.
STEPS = (
    "1.  Click “Open Steam login” and sign in to Steam in your browser.",
    "2.  In your browser open the developer tools (F12).",
    "3.  Find Cookies for store.steampowered.com (Application → Storage).",
    "4.  Copy the cookie list (or just sessionid and steamLoginSecure).",
    "5.  Paste it here and click Connect.",
)


class SteamLoginDialog(Gtk.Window):
    """Collects Steam login cookies captured in the user's own browser."""

    def __init__(
        self,
        store: SteamTokenStore,
        on_complete: Callable[[bool], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Connect Steam")
        self.store = store
        self._on_complete = on_complete
        self.set_default_size(600, 620)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        self.set_titlebar(_build_header_bar("Connect Steam"))
        self.set_child(self._build_body())

    # -- construction ---------------------------------------------------------

    def _build_body(self) -> Gtk.Widget:
        intro = Gtk.Label(
            label=(
                "Vitrine shows your full Steam library. Sign in once in your "
                "browser so Vitrine can authenticate as you."
            ),
            wrap=True,
            justify=Gtk.Justification.LEFT,
        )
        intro.set_margin_top(4)
        intro.set_margin_bottom(8)
        intro.set_margin_start(16)
        intro.set_margin_end(16)

        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        steps.set_margin_bottom(10)
        for step in STEPS:
            label = Gtk.Label(label=step, xalign=0.0)
            label.add_css_class("dim-label")
            label.set_margin_start(16)
            label.set_margin_end(16)
            steps.append(label)

        # Cookie paste area.
        self._cookies = Gtk.TextView()
        self._cookies.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._cookies.set_monospace(True)
        self._cookies.set_hexpand(True)
        self._cookies.set_vexpand(True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._cookies)
        scroller.set_vexpand(True)
        scroller.set_margin_top(6)
        scroller.set_margin_bottom(6)
        scroller.set_margin_start(16)
        scroller.set_margin_end(16)

        # Status message area (separate from the cookie input so a failure never
        # wipes what the user pasted).
        self._status = Gtk.Label(label="", wrap=True, xalign=0.0)
        self._status.set_margin_start(16)
        self._status.set_margin_end(16)
        self._status.add_css_class("dim-label")

        # Actions.
        open_button = Gtk.Button(label="Open Steam login")
        open_button.add_css_class("suggested-action")
        open_button.connect("clicked", self._on_open_login)

        paste_button = Gtk.Button(label="Connect")
        paste_button.connect("clicked", self._on_connect)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _b: self.close())

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_start(16)
        actions.set_margin_end(16)
        actions.set_margin_bottom(16)
        spacer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        spacer.set_hexpand(True)
        actions.append(cancel_button)
        actions.append(spacer)
        actions.append(open_button)
        actions.append(paste_button)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.append(intro)
        body.append(steps)
        body.append(scroller)
        body.append(self._status)
        body.append(actions)
        return body

    # -- behaviour ------------------------------------------------------------

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status.set_text(("Error: " if error else "") + message)
        self._status.set_css_classes(["dim-label"])

    def _on_open_login(self, _button: Gtk.Button) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(LOGIN_URL)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not open browser for Steam login: %s", error)
            self._set_status("Could not open the browser automatically.", error=True)

    def _on_connect(self, _button: Gtk.Button) -> None:
        buffer = self._cookies.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        jar = _parse_cookie_dump(text)
        if not jar.cookies:
            self._set_status("No cookies found. Paste the cookie list first.", error=True)
            return
        if not jar.get("steamLoginSecure") or not jar.get("sessionid"):
            self._set_status(
                "Paste at least sessionid and steamLoginSecure from "
                "store.steampowered.com.",
                error=True,
            )
            return
        try:
            self.store.set_credentials(jar)
            self._set_status("Fetching access token…")
            token = self.store.fetch_access_token()
            if not token:
                raise SteamAuthError("Sign-in succeeded but no access token was returned")
        except SteamAuthError as error:
            logger.warning("Steam connect failed: %s", error)
            self._set_status(f"Connection failed: {error}", error=True)
            return
        self._finish(True)

    def _finish(self, ok: bool) -> None:
        if self._on_complete is not None:
            self._on_complete(ok)
        if ok:
            self.close()


def _build_header_bar(title: str) -> Gtk.Widget:
    """A plain header bar used as the window's single title bar."""
    from gi.repository import Adw

    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle(title=title, subtitle=""))
    header.set_show_end_title_buttons(True)
    return header


def _parse_cookie_dump(text: str) -> CookieJar:
    """Parse user-pasted cookies.

    Accepts the Netscape-format lines produced by browser cookie exporters
    (``domain\t...\tname\tvalue``) and Chrome's ``Name=Value`` rows.
    The most recently pasted value for a cookie name wins.
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
        # Name=Value rows (Chrome devtools / simple paste).
        if "=" in line and not line.startswith("="):
            name, value = line.split("=", 1)
            jar.add({"name": name.strip(), "value": value.strip()})
    return jar