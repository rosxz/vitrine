"""Per-game achievements viewer window.

Lists a game's achievements (already stored/cached by the achievements service)
with their locked/unlocked icons, name, description, unlock date and progress,
plus a summary progress bar and a "Refresh" action that re-fetches from the
store provider. Modelled on :mod:`vitrine.ui.artwork_picker` where sensible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Adw, Gtk

from vitrine.services.achievements import fetch_achievements_cached, load_context
from vitrine.services.library import Game, Library

logger = logging.getLogger(__name__)

_FILTERS = (("all", "All"), ("unlocked", "Unlocked"), ("locked", "Locked"))
_LIST_ICON = 40


class AchievementsWindow(Gtk.Window):
    """Movable window showing one game's achievements."""

    def __init__(
        self,
        library: Library,
        game: Game,
        on_refreshed: Callable[[], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title=f"Achievements — {getattr(game, 'name', '')}")
        self.library = library
        self.game = game
        self._on_refreshed = on_refreshed
        self.add_css_class("vitrine-window")
        self.set_default_size(560, 560)
        if parent is not None:
            self.set_transient_for(parent)

        self._filter = "all"

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=self.get_title(), subtitle=""))
        header.set_show_end_title_buttons(True)
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        close.connect("clicked", lambda _b: self.close())
        header.pack_start(close)
        self._refresh_button = Gtk.Button(label="Refresh")
        self._refresh_button.connect("clicked", lambda _b: self.refresh())
        header.pack_end(self._refresh_button)
        self.set_titlebar(header)

        self._summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._summary.set_margin_top(16)
        self._summary.set_margin_bottom(4)
        self._summary.set_margin_start(20)
        self._summary.set_margin_end(20)

        self._summary_label = Gtk.Label(halign=Gtk.Align.START)
        self._summary_label.add_css_class("title-4")
        self._summary.append(self._summary_label)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        self._summary.append(self._progress)

        self._filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._filter_bar.set_margin_top(6)
        self._filter_bar.set_margin_start(20)
        self._filter_bar.set_margin_end(20)
        self._filter_group: Gtk.CheckButton | None = None
        self._filter_buttons: dict[str, Gtk.CheckButton] = {}
        for fid, label in _FILTERS:
            if self._filter_group is None:
                btn = Gtk.CheckButton(label=label, active=(fid == "all"))
                self._filter_group = btn
            else:
                btn = Gtk.CheckButton(label=label, group=self._filter_group)
            btn.connect("toggled", self._on_filter_toggled, fid)
            self._filter_buttons[fid] = btn
            self._filter_bar.append(btn)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._list)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(self._summary)
        content.append(self._filter_bar)
        content.append(scroller)
        self.set_child(content)

        self._populate()
        # If nothing is cached yet, fetch on first open so the window shows data
        # right away instead of an empty list (the user can also press Refresh).
        if not self.library.achievements_for(self.game):
            self.refresh()

    # -- data / rendering -----------------------------------------------------

    def _load(self):
        return self.library.achievements_for(self.game)

    def _populate(self) -> None:
        items = self._load()
        total = getattr(self.game, "achievement_count", 0) or len(items)
        unlocked = getattr(self.game, "achievement_unlocked", 0) or sum(1 for a in items if a.unlocked)
        self._summary_label.set_text(f"{unlocked} / {total} unlocked")
        self._progress.set_fraction((unlocked / total) if total else 0.0)
        self._progress.set_text(f"{int(round((unlocked / total) * 100)) if total else 0}%")

        self._clear_list()
        for ach in items:
            if self._filter == "unlocked" and not ach.unlocked:
                continue
            if self._filter == "locked" and ach.unlocked:
                continue
            self._list.append(self._row(ach))

    def _row(self, ach) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_top(8)
        row.set_margin_bottom(8)
        row.set_margin_start(20)
        row.set_margin_end(20)

        icon_path = ach.icon_unlocked_path if ach.unlocked else ach.icon_locked_path
        picture = Gtk.Picture()
        picture.set_size_request(_LIST_ICON, _LIST_ICON)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.add_css_class("achievement-icon")
        if icon_path:
            try:
                picture.set_filename(icon_path)
            except Exception:  # noqa: BLE001
                pass
        row.append(picture)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=ach.name or ach.key, halign=Gtk.Align.START)
        name.add_css_class("bold")
        name.set_xalign(0.0)
        text.append(name)
        if ach.description:
            desc = Gtk.Label(label=ach.description, wrap=True, halign=Gtk.Align.START, xalign=0.0)
            desc.add_css_class("dim-label")
            text.append(desc)
        if ach.unlock_date:
            from datetime import datetime

            stamp = Gtk.Label(
                label=f"Unlocked {datetime.fromtimestamp(ach.unlock_date):%Y-%m-%d}",
                halign=Gtk.Align.START,
                xalign=0.0,
            )
            stamp.add_css_class("caption")
            text.append(stamp)
        row.append(text)
        if ach.hidden and not ach.unlocked:
            tag = Gtk.Label(label="Hidden")
            tag.add_css_class("caption")
            row.append(tag)
        return row

    def _clear_list(self) -> None:
        while child := self._list.get_first_child():
            self._list.remove(child)

    # -- events ---------------------------------------------------------------

    def _on_filter_toggled(self, btn: Gtk.CheckButton, fid: str) -> None:
        if btn.get_active():
            self._filter = fid
            self._populate()

    def refresh(self) -> None:
        """Re-fetch achievements from the store on a worker thread.

        The network fetch + icon download happen off-thread (no DB); the sqlite
        write is marshalled back to the GTK loop since the connection is
        main-thread-only.
        """
        self._refresh_button.set_sensitive(False)
        self._refresh_button.set_label("Refreshing…")

        ctx = load_context(self.library)

        def _worker() -> None:
            result = None
            try:
                result = fetch_achievements_cached(self.game, ctx)
            except Exception:  # noqa: BLE001
                logger.exception("refreshing achievements for %s", getattr(self.game, "name", ""))
            finally:
                from gi.repository import GLib

                GLib.idle_add(self._on_refresh_done, result)

        import threading

        threading.Thread(target=_worker, daemon=True).start()

    def _on_refresh_done(self, result=None) -> None:
        self._refresh_button.set_sensitive(True)
        self._refresh_button.set_label("Refresh")
        if result is not None and result.achievements:
            # Persist on the main thread; drop stale rows absent from the payload.
            self.library.replace_achievements(self.game, result)
        self._populate()
        if self._on_refreshed is not None:
            self._on_refreshed()


# Alias to avoid a circular import with the detail bar.
AchievementsViewer = AchievementsWindow