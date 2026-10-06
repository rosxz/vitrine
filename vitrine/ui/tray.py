"""System-tray icon for Vitrine, built directly on D-Bus.

GTK4 dropped ``Gtk.StatusIcon`` and libadwaita ships no tray widget. Rather than
pull the GTK3-based AppIndicator libraries into a GTK4 process, this module
speaks the two D-Bus protocols a tray host actually understands:

* ``org.kde.StatusNotifierItem`` -- the icon and its callbacks. ``Activate``
  (a left click on hosts that honour it, a double click on GNOME) opens the
  window; ``SecondaryActivate`` (middle click) also opens it.
* ``com.canonical.dbusmenu`` -- the native menu (open, last game played,
  settings, quit), rendered by the host.

It uses only ``Gio``'s D-Bus support, so there is no extra runtime dependency.
When no session bus or ``StatusNotifierWatcher`` is present the tray is marked
unavailable and the rest of the app keeps working.
"""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Callable

from gi.repository import Gio, GLib

from vitrine import APP_ID, APP_NAME
from vitrine.services.library import Game

logger = logging.getLogger(__name__)

#: Object paths the icon and its menu are exported on.
SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

#: DBusMenu item ids.
_ITEM_OPEN = 1
_ITEM_LAST = 2
_ITEM_SEP = 3
_ITEM_SETTINGS = 4
_ITEM_QUIT = 5

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconPixmap" type="a(ayay)" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
  </interface>
</node>
"""


class TrayIcon:
    """A StatusNotifierItem with a DBusMenu, exposing the app's tray actions."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        last_game_provider: Callable[[], Game | None],
        on_play_last: Callable[[Game], None],
    ) -> None:
        self._on_open = on_open
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._last_game_provider = last_game_provider
        self._on_play_last = on_play_last
        self._revision = 1
        self._registrations: list[int] = []
        self._owner_id = 0
        self._connection: Gio.DBusConnection | None = None
        #: True only when a tray host actually accepted the item.
        self.available = False
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            logger.info("system tray disabled: no session bus (%s)", exc.message)
            return
        self._register()

    # -- setup ----------------------------------------------------------------

    def _register(self) -> None:
        connection = self._connection
        assert connection is not None
        sni_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
        # PyGObject's callable-based register_object is the only binding that
        # accepts Python callbacks; the C API marks it deprecated in favour of a
        # GClosure variant we can't feed Python functions to.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for path, info, on_call, on_prop in (
                (SNI_PATH, sni_info, self._on_sni_call, self._on_sni_property),
                (MENU_PATH, menu_info, self._on_menu_call, self._on_menu_property),
            ):
                try:
                    self._registrations.append(
                        connection.register_object(path, info, on_call, on_prop, None)
                    )
                except GLib.Error as exc:
                    logger.warning("could not export tray object %s: %s", path, exc.message)
        self._owner_id = Gio.bus_own_name_on_connection(
            connection, self._bus_name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self.available = self._notify_watcher()

    def close(self) -> None:
        """Release the exported objects and bus name (on app shutdown)."""
        connection = self._connection
        if connection is None:
            return
        for registration in self._registrations:
            try:
                connection.unregister_object(registration)
            except GLib.Error:
                pass
        self._registrations.clear()
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        self.available = False

    def _notify_watcher(self) -> bool:
        """Register with the host's watcher; False when there's no tray host."""
        connection = self._connection
        assert connection is not None
        try:
            connection.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            return True
        except GLib.Error as exc:
            logger.info("system tray disabled: no StatusNotifierWatcher (%s)", exc.message)
            return False

    def notify_changed(self) -> None:
        """Tell the host the menu contents changed (e.g. a new last-played game)."""
        self._revision += 1
        connection = self._connection
        if connection is None or not self.available:
            return
        try:
            connection.emit_signal(
                None,
                MENU_PATH,
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)),
            )
        except GLib.Error:  # noqa: BLE001 - best effort, the host may be gone
            logger.debug("could not emit LayoutUpdated")

    # -- StatusNotifierItem ----------------------------------------------------

    def _on_sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        # Activate is the primary "open" click. Hosts that honour ItemIsMenu=False
        # (KDE, waybar, ...) send it on a single left click; the GNOME AppIndicator
        # extension ignores ItemIsMenu and only sends it on a double click, but it
        # sends SecondaryActivate on a middle click. Both open the window.
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._on_open)
        invocation.return_value(None)

    def _on_sni_property(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_NAME.lower()),
            "Title": GLib.Variant("s", APP_NAME),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", APP_ID),
            "IconThemePath": GLib.Variant("s", ""),
            "Menu": GLib.Variant("o", MENU_PATH),
            "ItemIsMenu": GLib.Variant("b", False),
            "IconPixmap": GLib.Variant("a(ayay)", []),
        }
        return values.get(name)

    # -- com.canonical.dbusmenu ------------------------------------------------

    def _on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(
                GLib.Variant("(u(ia{sv}av))", (self._revision, self._layout()))
            )
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            entries = [(item_id, self._props_for(item_id)) for item_id in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (entries,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = self._props_for(item_id).get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                GLib.idle_add(self._activate, item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            events, = params.unpack()
            for item_id, event_id, _data, _ts in events:
                if event_id == "clicked":
                    GLib.idle_add(self._activate, item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _on_menu_property(self, _conn, _sender, _path, _iface, name):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(name)

    def _layout(self) -> tuple:
        """The full menu tree as ``(id, props, children)`` (the root is id 0)."""
        game = self._last_game_provider()
        last_label = f"Play {game.name}" if game is not None else "No recent game"
        items = [
            self._menu_item(_ITEM_OPEN, label="Open Vitrine"),
            self._menu_item(_ITEM_LAST, label=last_label, enabled=game is not None),
            self._menu_item(_ITEM_SEP, separator=True),
            self._menu_item(_ITEM_SETTINGS, label="Settings"),
            self._menu_item(_ITEM_QUIT, label="Quit Vitrine"),
        ]
        return (0, {}, items)

    @staticmethod
    def _menu_item(
        item_id: int, *, label: str = "", enabled: bool = True, separator: bool = False
    ) -> GLib.Variant:
        if separator:
            props = {"type": GLib.Variant("s", "separator")}
        else:
            props = {
                "label": GLib.Variant("s", label),
                "enabled": GLib.Variant("b", enabled),
                "visible": GLib.Variant("b", True),
            }
        return GLib.Variant("(ia{sv}av)", (item_id, props, []))

    def _props_for(self, item_id: int) -> dict:
        game = self._last_game_provider()
        if item_id == _ITEM_OPEN:
            return {
                "label": GLib.Variant("s", "Open Vitrine"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        if item_id == _ITEM_LAST:
            return {
                "label": GLib.Variant(
                    "s", f"Play {game.name}" if game is not None else "No recent game"
                ),
                "enabled": GLib.Variant("b", game is not None),
                "visible": GLib.Variant("b", True),
            }
        if item_id == _ITEM_SEP:
            return {"type": GLib.Variant("s", "separator")}
        if item_id == _ITEM_SETTINGS:
            return {
                "label": GLib.Variant("s", "Settings"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        if item_id == _ITEM_QUIT:
            return {
                "label": GLib.Variant("s", "Quit Vitrine"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        return {}

    def _activate(self, item_id: int) -> None:
        if item_id == _ITEM_OPEN:
            self._on_open()
        elif item_id == _ITEM_LAST:
            game = self._last_game_provider()
            if game is not None:
                self._on_play_last(game)
        elif item_id == _ITEM_SETTINGS:
            self._on_settings()
        elif item_id == _ITEM_QUIT:
            self._on_quit()
