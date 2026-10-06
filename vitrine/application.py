"""The GTK application object.

The ``gi.require_version`` calls must run before anything imports
``gi.repository``, so this module deliberately imports below them.
"""

# ruff: noqa: E402

from __future__ import annotations

import logging
import sqlite3

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib

from vitrine import APP_ID, APP_NAME
from vitrine.infra import paths
from vitrine.infra.db import connect, initialize
from vitrine.services.library import Library
from vitrine.ui import VitrineWindow
from vitrine.ui.theme import ThemeManager

logger = logging.getLogger(__name__)


class VitrineApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connection: sqlite3.Connection | None = None
        self.library: Library | None = None
        self.theme_manager = ThemeManager()
        #: The single main window (kept even while hidden to the tray).
        self.window: VitrineWindow | None = None
        #: System-tray icon (StatusNotifierItem), created with the first window.
        self.tray = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)

        paths.ensure_dirs()
        logging.basicConfig(level=logging.INFO, filename=str(paths.log_path()))

        self.connection = connect()
        initialize(self.connection)
        self.library = Library(self.connection)
        self.library.migrate_artwork_source_default()
        self.theme_manager.apply(self.library.setting("theme", "galaxy"))
        logger.info("%s started", APP_NAME)

    def do_activate(self) -> None:
        if self.library is None:
            raise RuntimeError("Application activated before startup completed")

        if self.window is None:
            self.window = VitrineWindow(application=self, library=self.library)
        self.window.present()
        if self.tray is None:
            self._create_tray()

    # -- system tray -----------------------------------------------------------

    def _create_tray(self) -> None:
        from vitrine.ui.tray import TrayIcon

        self.tray = TrayIcon(
            on_open=self.present_window,
            on_settings=self._open_settings,
            on_quit=self.quit,
            last_game_provider=self._last_game,
            on_play_last=self._play_last,
        )

    def present_window(self) -> None:
        """Show and focus the window (tray activation / double-click)."""
        if self.window is None:
            self.activate()
        else:
            self.window.present()

    def _open_settings(self) -> None:
        if self.window is not None:
            self.window.on_settings_clicked(None)

    def _last_game(self):
        return self.window.last_played_game() if self.window is not None else None

    def _play_last(self, game) -> None:
        if self.window is None:
            self.activate()
        if self.window is not None:
            self.window.play_last_game(game)

    def refresh_tray_menu(self) -> None:
        """Refresh the tray's menu (e.g. after the last-played game changes)."""
        if self.tray is not None:
            self.tray.notify_changed()

    def quit(self) -> None:
        """Quit for real: let the window close instead of hiding to the tray."""
        if self.window is not None:
            self.window.prepare_quit()
        super().quit()


    def do_shutdown(self) -> None:
        if self.tray is not None:
            self.tray.close()
            self.tray = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    return VitrineApplication().run(argv)
