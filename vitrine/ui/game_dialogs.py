"""Dialogs for adding and editing a game.

Both the "add a game" dialog and the per-game settings use the shared
:class:`GameForm`, wiring its browse buttons to GTK's native file chooser.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..library import Game, Library
from .game_form import GameForm, _LabeledEntry

_BROWSE_TITLES: dict[str, str] = {
    "executable": "Choose the game executable",
    "cover": "Choose the cover image",
    "banner": "Choose the banner image",
}

#: Browse kinds restricted to images (as opposed to any file for the executable).
_IMAGE_KINDS = {"cover", "banner"}


def _parent_window(widget: Gtk.Widget | None) -> Gtk.Window | None:
    if isinstance(widget, Gtk.Window):
        return widget
    if widget is None:
        return None
    root = widget.get_root()
    return root if isinstance(root, Gtk.Window) else None


def _open_picker(
    parent: Gtk.Widget | None,
    kind: str,
    accept: Callable[[str], None],
) -> None:
    """Open a native file chooser and forward the selected path."""
    chooser = Gtk.FileDialog(title=_BROWSE_TITLES[kind])
    if kind in _IMAGE_KINDS:
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/avif", "image/bmp"):
            image_filter.add_mime_type(mime)
        chooser.set_default_filter(image_filter)

    def on_selected(dialog: Gtk.FileDialog, result: object) -> None:
        try:
            file = dialog.open_finish(result)
        except (GLib.Error, TypeError):  # cancelled
            return
        if file is not None and file.get_path():
            accept(file.get_path())

    chooser.open(_parent_window(parent), None, on_selected)


class _GameDialog(Adw.Dialog):
    """Shared shell placing a :class:`GameForm` between a header and buttons."""

    def __init__(
        self,
        library: Library,
        title: str,
        form: GameForm,
        parent: Gtk.Widget | None,
    ) -> None:
        super().__init__()
        self.library = library
        self._form = form
        self._parent = parent
        self.set_title(title)
        self.set_content_width(520)

        body = Gtk.ScrolledWindow()
        body.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.set_child(form)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(16)
        body.set_margin_end(16)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _btn: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label=self._save_label(), css_classes=["suggested-action"])
        save.connect("clicked", self._on_save)
        header.pack_end(save)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.set_child(toolbar)

        for kind in _BROWSE_TITLES:
            form.connect_browse(kind, self._make_browse(kind))

    def _make_browse(self, kind: str) -> Callable[[_LabeledEntry], None]:
        def accept(path: str) -> None:
            self._form.set_browse_result(kind, path)

        def open_chooser(entry: _LabeledEntry) -> None:
            _open_picker(self._parent, kind, accept)

        return open_chooser

    def _save_label(self) -> str:
        return "Save"

    def _on_save(self, _button: Gtk.Button) -> None:
        if not self._form.validate():
            game = self.save()
            if game is not None:
                self.close()


class AddGameDialog(_GameDialog):
    """Create a new local game and notify the caller when it is saved."""

    def __init__(
        self,
        library: Library,
        on_add: Callable[[Game], None],
        parent: Gtk.Widget | None = None,
    ) -> None:
        self._on_add = on_add
        super().__init__(library, title="Add a game", form=GameForm(), parent=parent)

    def _save_label(self) -> str:
        return "Add game"

    def save(self) -> Game:
        game = self._form.build_game()
        self._on_add(game)
        return game


class GameSettingsDialog(_GameDialog):
    """Edit an existing game's metadata and artwork."""

    def __init__(
        self,
        library: Library,
        game: Game,
        on_save: Callable[[Game], None],
        parent: Gtk.Widget | None = None,
    ) -> None:
        self._game = game
        self._on_save = on_save
        form = GameForm()
        form.populate(game)
        super().__init__(library, title=f"Edit {game.name}", form=form, parent=parent)

    def save(self) -> Game:
        game = self._form.apply_to(self._game)
        self.library.update(game)
        self._on_save(game)
        return game