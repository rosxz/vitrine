"""Dialog for adding a game by hand."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..library import Game


class AddGameDialog(Adw.Dialog):
    """Asks for the minimum needed to launch something: name and executable."""

    def __init__(self, on_add: Callable[[Game], None], parent: Gtk.Widget | None = None) -> None:
        super().__init__()
        self._on_add = on_add
        self.set_title("Add a game")
        self.set_content_width(480)

        self.name_row = Adw.EntryRow(title="Name")
        self.executable_row = Adw.EntryRow(title="Executable")
        self.arguments_row = Adw.EntryRow(title="Arguments")
        self.prefix_row = Adw.EntryRow(title="Wine prefix (optional)")

        browse = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER)
        browse.add_css_class("flat")
        browse.set_tooltip_text("Choose the game executable")
        browse.connect("clicked", self._on_browse_clicked)
        self.executable_row.add_suffix(browse)

        group = Adw.PreferencesGroup()
        group.add(self.name_row)
        group.add(self.executable_row)
        group.add(self.arguments_row)
        group.add(self.prefix_row)

        page = Adw.PreferencesPage()
        page.add(group)

        self.add_button = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        self.add_button.add_css_class("suggested-action")
        self.add_button.connect("clicked", self._on_add_clicked)

        header = Adw.HeaderBar()
        header.pack_start(Gtk.Button(label="Cancel", valign=Gtk.Align.CENTER, visible=True))
        header.pack_end(self.add_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)

        self.name_row.connect("changed", self._update_sensitivity)
        self.executable_row.connect("changed", self._update_sensitivity)
        self._update_sensitivity()

    def _update_sensitivity(self, *_args: object) -> None:
        self.add_button.set_sensitive(
            bool(self.name_row.get_text().strip()) and bool(self.executable_row.get_text().strip())
        )

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        def on_selected(dialog: Gtk.FileDialog, result: object) -> None:
            try:
                file = dialog.open_finish(result)
            except Exception:  # cancelled or failed; nothing to do
                return
            if file:
                path = file.get_path() or ""
                self.executable_row.set_text(path)
                if not self.name_row.get_text().strip():
                    self.name_row.set_text(file.get_basename() or "")

        chooser = Gtk.FileDialog(title="Choose the game executable")
        chooser.open(self, None, on_selected)

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        game = Game(
            name=self.name_row.get_text().strip(),
            runner="wine",
            executable=self.executable_row.get_text().strip() or None,
            arguments=self.arguments_row.get_text().strip() or None,
            prefix=self.prefix_row.get_text().strip() or None,
            source="local",
        )
        self._on_add(game)
        self.close()
