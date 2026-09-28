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

        window = self.props.active_window
        if window is None:
            window = VitrineWindow(application=self, library=self.library)
        window.present()

    def do_shutdown(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    return VitrineApplication().run(argv)
