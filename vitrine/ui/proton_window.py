"""Proton / Wine runner manager window.

Lists the discovered Wine/Proton builds (mirroring Lutris' runner manager):
installed runners get a "Remove" button (removes only Vitrine's registration,
never the files on disk), and the "installable" section offers Lutris Wine-GE
builds that have not been added yet. Selecting a runner sets the *default*
runner used by the app (overridable per game).
"""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gtk

from ..library import Library
from ..runners import (
    DEFAULT_PROTON_SETTING,
    install_runner,
    list_runners,
    load_runners_store,
    remove_runner,
    save_runners_store,
)

logger = logging.getLogger(__name__)


class ProtonWindow(Gtk.Window):
    """A movable window to manage Wine/Proton runners and set the default."""

    def __init__(
        self,
        library: Library,
        on_changed: object | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title="Proton & Wine runners")
        self.library = library
        self._on_changed = on_changed
        self.set_default_size(520, 620)
        self.add_css_class("vitrine-window")
        if parent is not None:
            self.set_transient_for(parent)

        self._runners_store = load_runners_store(library)
        self._default = str(self.library.setting(DEFAULT_PROTON_SETTING, "wine-64") or "wine-64")

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Proton & Wine", subtitle=""))
        header.set_show_end_title_buttons(True)
        close = Gtk.Button(label="Done")
        close.add_css_class("suggested-action")
        close.connect("clicked", lambda _b: self.close())
        header.pack_start(close)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._body.set_margin_top(12)
        self._body.set_margin_bottom(12)
        self._body.set_margin_start(16)
        self._body.set_margin_end(16)

        self._default_row = self._build_default_selector()
        self._body.append(self._default_row)

        self._installed_list = self._build_installed_section()
        self._body.append(self._installed_list)

        self._available_row = self._build_available_section()
        self._body.append(self._available_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._body)
        scroller.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(scroller)

        self.set_titlebar(header)
        self.set_child(content)
        self._refresh_lists()

    # -- sections -------------------------------------------------------------

    def _build_default_selector(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Default Proton / Wine")

        row = Adw.ComboRow(title="Default runner")
        row.set_subtitle("Used for games that don't override it")
        self._default_row_widget = row
        group.add(row)
        self._build_default_model([r.id for r in list_runners(self._runners_store)])
        return group

    def _build_default_model(self, runner_ids: list[str]) -> None:
        names: dict[str, str] = {r.id: r.name for r in list_runners(self._runners_store)}
        model = Gtk.StringList.new([names.get(i, i) for i in runner_ids])
        self._default_row_widget.set_model(model)
        self._default_ids = list(runner_ids)
        if self._default in runner_ids:
            self._default_row_widget.set_selected(self._default_ids.index(self._default))
        elif runner_ids:
            self._default_row_widget.set_selected(0)
        self._default_row_widget.connect("notify::selected-item", self._on_default_selected)

    def _build_installed_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Installed runners")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._installed_box = box
        group.add(_row_widget(box))
        return group

    def _build_available_section(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Installable (Lutris Wine-GE)")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._available_box = box
        hint = Gtk.Label(label="Choose a runner to download and install.", wrap=True, xalign=0.0)
        hint.add_css_class("dim-label")
        self._available_hint = hint
        box.append(hint)
        group.add(_row_widget(box))
        return group

    # -- refresh --------------------------------------------------------------

    def _refresh_lists(self) -> None:
        runners = list_runners(self._runners_store)
        # Default selector
        self._build_default_model([r.id for r in runners])

        # Installed list
        self._clear(self._installed_box)
        presets = [r for r in runners if r.is_preset]
        installed = [r for r in runners if not r.is_preset]
        for runner in presets + installed:
            self._installed_box.append(self._installed_row(runner))

        if not installed:
            note = Gtk.Label(label="No downloaded runners yet — add one from below.", wrap=True, xalign=0.0)
            note.add_css_class("dim-label")
            self._installed_box.append(note)

        # Available list (populate async to avoid blocking the UI thread).
        self._clear(self._available_box)
        self._available_box.append(self._available_hint)
        threading.Thread(target=self._load_available, daemon=True, name="proton-available").start()

    def _load_available(self) -> None:
        try:
            from ..runners_source import available_runners

            available = available_runners()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load installable runners: %s", exc)
            available = []
        installed = {r.id for r in list_runners(self._runners_store)}

        def _render() -> None:
            entries = [a for a in available if a["id"] not in installed]
            GLib_idle(self._populate_available, entries)

        _render()

    def _populate_available(self, entries: list[dict]) -> None:
        for entry in entries:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=entry["name"], hexpand=True, xalign=0.0)
            install = Gtk.Button(label="Install")
            install.connect("clicked", lambda _b, e=entry: self._install_available(e))
            row.append(label)
            row.append(install)
            self._available_box.append(row)

    # -- rows ------------------------------------------------------------------

    def _installed_row(self, runner) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=runner.name, hexpand=True, xalign=0.0)
        row.append(label)
        if not runner.is_preset:
            remove = Gtk.Button(label="Remove")
            remove.add_css_class("destructive-action")
            remove.connect("clicked", lambda _b, rid=runner.id: self._remove(rid))
            row.append(remove)
        return row

    # -- actions ---------------------------------------------------------------

    def _on_default_selected(self, row: Gtk.ComboRow, _pspec: object) -> None:
        index = row.get_selected()
        if 0 <= index < len(self._default_ids):
            self._default = self._default_ids[index]
            self.library.set_setting(DEFAULT_PROTON_SETTING, self._default)

    def _remove(self, runner_id: str) -> None:
        self._runners_store = remove_runner(runner_id, self._runners_store)
        self._persist()
        self._refresh_lists()

    def _install_available(self, entry: dict) -> None:
        from ..runners_source import download_runner

        def _work() -> None:
            try:
                path = download_runner(entry)
                self._runners_store = install_runner(entry["id"], path, self._runners_store)
                self._persist()
            except Exception as exc:  # noqa: BLE001
                logger.error("Download failed for %s: %s", entry["id"], exc)

        threading.Thread(target=_work, daemon=True, name="proton-download").start()

    def _persist(self) -> None:
        save_runners_store(self.library, self._runners_store)
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                logger.exception("Proton change handler failed")

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        while child := box.get_first_child():
            box.remove(child)


def GLib_idle(fn, *args) -> None:
    from gi.repository import GLib

    GLib.idle_add(fn, *args)


def _row_widget(widget: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.set_margin_top(4)
    box.set_margin_bottom(4)
    box.set_margin_start(16)
    box.set_margin_end(16)
    box.append(widget)
    return box