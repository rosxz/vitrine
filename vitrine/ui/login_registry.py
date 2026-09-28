# ruff: noqa: E402

"""Map a source id to its login dialog (UI layer).

Sources stay GUI-free, so this registry is the single place that turns a source
into the ``WebKitLoginDialog`` used to sign in. The window consults it rather than
hand-writing a ``steam/gog/epic`` switch.
"""

from __future__ import annotations

import gi

gi.require_version("WebKit", "6.0")

from gi.repository import Gtk  # noqa: E402

from vitrine.ui.epic_login_dialog import EpicLoginDialog
from vitrine.ui.gog_login_dialog import GogLoginDialog
from vitrine.ui.steam_login_dialog import SteamLoginDialog


def make_login_dialog(source_id: str, store, on_complete, parent: Gtk.Window | None = None):
    """Build the login dialog for ``source_id`` from its token ``store``.

    ``on_complete`` receives ``(ok, ...extras)``; each store passes what its
    login flow needs (Steam: nothing; GOG: user_id; Epic: account_id + code).
    """
    if source_id == "steam":
        return SteamLoginDialog(store, on_complete=on_complete, parent=parent)
    if source_id == "gog":
        return GogLoginDialog(store, on_complete=on_complete, parent=parent)
    if source_id == "epic":
        return EpicLoginDialog(store, on_complete=on_complete, parent=parent)
    raise ValueError(f"No login dialog for source: {source_id}")