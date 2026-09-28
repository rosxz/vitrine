"""Shared embedded-WebKit login dialog chrome (GTK).

Every store login signs the user in through a real ``WebKitWebView`` inside a
window. This base owns the common chrome — window setup, the webview and its
media-disabled settings, the header with a reset button, the status banner, and
the poll/finish/reset skeleton — so each store's dialog only implements its
provider-specific capture (Steam cookie polling, GOG redirect code, Epic body
parse). Please keep ``gi.require_version("WebKit", "6.0")`` pinned before any
``gi.repository`` import, as the concrete dialogs do.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("WebKit", "6.0")

from gi.repository import Adw, GLib, Gtk, WebKit  # noqa: E402

logger = logging.getLogger(__name__)


class WebKitLoginDialog(Gtk.Window):
    """An embedded WebKit window for signing into a store.

    Subclasses must set :attr:`login_url` (the URL to load) and hook the reset
    behaviour (via :meth:`_on_reset`), and run the actual credential capture
    once the poller (:meth:`_poll_tick`) detects the login completed — then call
    :meth:`finish`(:code:`True`) (or :code:`False` to keep the window open).
    """

    #: URL the webview loads on construction and after a reset.
    login_url: str = ""

    #: Window title.
    dialog_title: str = "Sign in"

    def __init__(
        self,
        on_complete: Callable[..., None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title=self.dialog_title)
        self._on_complete = on_complete
        self._poll_id: int | None = None
        self._capturing = False

        self.set_default_size(720, 860)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        self.webview = WebKit.WebView()
        self.webview.connect("load_changed", self._on_load_changed)
        self.webview.connect("create", self._on_create_popup)
        self.webview.set_vexpand(True)
        self.webview.set_hexpand(True)
        # Store logins need nothing from WebKit's media/GStreamer stack; disabling
        # it avoids the web process spamming GStreamer GPU criticals.
        web_settings = WebKit.Settings()
        for prop in ("enable-media", "enable-mediasource", "enable-webaudio", "enable-webgl", "enable-media-stream"):
            setter = "set_" + prop
            if hasattr(web_settings, setter):
                getattr(web_settings, setter)(False)
        self.webview.set_settings(web_settings)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=self.dialog_title, subtitle=""))
        header.set_show_end_title_buttons(True)
        reset_button = Gtk.Button(label="Reset session")
        reset_button.set_tooltip_text("Clear stored login and cookies, then start over")
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

        self.webview.load_uri(self.login_url)
        self._start_polling()

    # -- WebKit callbacks -----------------------------------------------------

    def _on_load_changed(self, _webview: WebKit.WebView, load_event: WebKit.LoadEvent) -> None:
        # A page load is a convenient kick for the poller, but the poller is the
        # real detector (a QR login may not navigate to the success URI).
        if load_event == WebKit.LoadEvent.FINISHED:
            self._kick_poll()

    def _on_create_popup(
        self, _webview: WebKit.WebView, _navigation: WebKit.NavigationAction
    ) -> WebKit.WebView | None:
        # Return None so the target opens in this same view, keeping the cookie
        # session intact and avoiding ownership/GC issues from swapping children.
        return None

    # -- polling ---------------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add(self.poll_interval_ms(), self._poll_tick)

    def _kick_poll(self) -> None:
        self._poll_tick()

    def _stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def poll_interval_ms(self) -> int:
        return 800

    def _poll_tick(self) -> bool:
        """Keep polling (return True) while the dialog is alive."""
        if not self._capturing:
            self._poll()
        return True

    def _poll(self) -> None:
        """Per-provider capture check; called on every poll tick."""

    # -- status / finish / reset -----------------------------------------------

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status.set_text(("Error: " if error else "") + message)
        self._status.set_visible(bool(message))

    def finish(self, ok: bool, *args) -> None:
        """Stop the poller and close the window, invoking ``on_complete``.

        ``args`` are forwarded straight to ``on_complete`` (each store passes
        what its login flow needs, e.g. an account id).
        """
        self._stop_polling()
        self._capturing = True
        if self._on_complete is not None:
            try:
                self._on_complete(ok, *args)
            except Exception:  # noqa: BLE001 - never crash closing a login
                logger.exception("login complete callback failed")
        self.close()

    def reset_session(self, _button: Gtk.Button | None = None) -> None:
        """Reset the store's credentials and reload the login page."""
        self._capturing = False
        self._stop_polling()
        self._on_reset()
        self._set_status("")
        self.webview.load_uri(self.login_url)
        self._start_polling()

    def _on_reset(self) -> None:
        """Provide the reset side-effects (clear the durable store/credentials)."""