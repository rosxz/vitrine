"""Guard against GTK/libadwaita API drift.

These tests only import the GUI modules and check that the widgets the UI relies
on still exist, which catches typos and version regressions without needing a
display. The GTK import has to follow the version pins.
"""

# ruff: noqa: E402

from __future__ import annotations

import pytest

from vitrine.ui import library_view

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk


def test_gui_modules_import() -> None:
    import vitrine.application  # noqa: F401
    import vitrine.ui.add_game_dialog  # noqa: F401
    import vitrine.ui.library_view  # noqa: F401
    import vitrine.ui.window  # noqa: F401


@pytest.mark.parametrize(
    "widget",
    ["ToolbarView", "OverlaySplitView", "ToastOverlay", "StatusPage", "HeaderBar", "WindowTitle", "EntryRow"],
)
def test_libadwaita_widgets_are_available(widget: str) -> None:
    assert hasattr(Adw, widget), f"libadwaita is missing Adw.{widget}"


@pytest.mark.parametrize("widget", ["AspectFrame", "FlowBox", "FlowBoxChild", "Picture", "FileDialog", "ContentFit"])
def test_gtk_widgets_are_available(widget: str) -> None:
    assert hasattr(Gtk, widget), f"GTK is missing Gtk.{widget}"


def test_cover_tiles_are_portrait() -> None:
    assert library_view.COVER_RATIO < 1, "covers must be portrait (2:3)"
