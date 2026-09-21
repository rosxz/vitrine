"""Windows for adding and editing a game.

Both the "add a game" window and the per-game settings use the shared
:class:`GameForm`, wiring its browse buttons to GTK's native file chooser.
They are top-level :class:`Gtk.Window` instances so they can be moved around
independently of the main window.
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

_FORM_WIDTH = 680
_FORM_HEIGHT = 780


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


class _GameWindow(Gtk.Window):
    """Shared movable window placing a :class:`GameForm` under a header.

    The save button signals ``_on_save`` (the method); the caller-provided
    callback that fires once a game is saved is stored separately as
    ``_save_callback`` -- never under ``_on_save``, which would shadow the
    method and pass the button through to it.
    """

    def __init__(
        self,
        library: Library,
        title: str,
        form: GameForm,
        parent: Gtk.Widget | None,
        on_remove: Callable[[Game], None] | None = None,
        on_refresh_artwork: Callable[[Game], None] | None = None,
        on_wine_config: Callable[[Game], None] | None = None,
    ) -> None:
        super().__init__(title=title)
        self.library = library
        self._form = form
        self._save_callback: Callable[[Game], None] | None = None
        self._remove_callback = on_remove
        self._refresh_artwork_callback = on_refresh_artwork
        self._wine_config_callback = on_wine_config
        self.add_css_class("vitrine-window")
        self.set_default_size(_FORM_WIDTH, _FORM_HEIGHT)
        parent_window = _parent_window(parent)
        if parent_window is not None:
            self.set_transient_for(parent_window)

        body = Gtk.ScrolledWindow()
        body.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.set_child(form)
        body.set_vexpand(True)
        body.set_hexpand(True)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(16)
        body.set_margin_end(16)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=""))
        header.set_show_end_title_buttons(True)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _btn: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label=self._save_label(), css_classes=["suggested-action"])
        save.connect("clicked", self._on_save)
        header.pack_end(save)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(body)
        if (
            on_remove is not None
            or self._refresh_artwork_callback is not None
            or self._wine_config_callback is not None
        ):
            content.append(self._build_footer())

        self.set_titlebar(header)
        self.set_child(content)

        for kind in _BROWSE_TITLES:
            form.connect_browse(kind, self._make_browse(kind))

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(8)
        footer.set_margin_bottom(12)
        footer.set_margin_start(16)
        footer.set_margin_end(16)

        remove = Gtk.Button(label="Remove from library")
        remove.add_css_class("destructive-action")
        remove.set_tooltip_text("Delete this entry from Vitrine (the installed files are untouched)")
        remove.connect("clicked", self._on_remove)
        footer.append(remove)
        if self._refresh_artwork_callback is not None:
            refresh = Gtk.Button(label="Refresh artwork")
            refresh.set_tooltip_text("Re-download automatic artwork for this game")
            refresh.connect("clicked", self._on_refresh_artwork)
            footer.append(refresh)
        if self._wine_config_callback is not None:
            winecfg = Gtk.Button(label="Wine Configuration…")
            winecfg.set_tooltip_text("Open winecfg for this game's prefix (deps, drives, environment)")
            winecfg.connect("clicked", self._on_wine_config)
            footer.append(winecfg)
        return footer

    def _on_wine_config(self, _button: Gtk.Button) -> None:
        if self._wine_config_callback is None:
            return
        game = self._remove_game()
        if game is not None:
            self._wine_config_callback(game)

    def _on_refresh_artwork(self, _button: Gtk.Button) -> None:
        if self._refresh_artwork_callback is None:
            return
        game = self._remove_game()
        if game is not None:
            self._refresh_artwork_callback(game)

    def _on_remove(self, _button: Gtk.Button) -> None:
        if self._remove_callback is None:
            return
        game = self._remove_game()
        if game is not None:
            self._remove_callback(game)
            self.close()

    def _remove_game(self) -> Game | None:
        return None

    def _make_browse(self, kind: str) -> Callable[[_LabeledEntry], None]:
        def accept(path: str) -> None:
            self._form.set_browse_result(kind, path)

        def open_chooser(entry: _LabeledEntry) -> None:
            _open_picker(self, kind, accept)

        return open_chooser

    def _save_label(self) -> str:
        return "Save"

    def _on_save(self, _button: Gtk.Button) -> None:
        if not self._form.validate():
            game = self.save()
            if game is not None:
                self.close()


class AddGameWindow(_GameWindow):
    """Create a new local game and notify the caller when it is saved."""

    def __init__(
        self,
        library: Library,
        on_add: Callable[[Game], None],
        parent: Gtk.Widget | None = None,
    ) -> None:
        super().__init__(
            library,
            title="Add a game",
            form=GameForm(allow_provider=False, **_runner_form_args(library)),
            parent=parent,
        )
        self._save_callback = on_add

    def _save_label(self) -> str:
        return "Add game"

    def save(self) -> Game:
        game = self._form.build_game()
        if self._save_callback is not None:
            self._save_callback(game)
        return game


class GameSettingsWindow(_GameWindow):
    """Edit an existing game's metadata and artwork in a movable window."""

    def __init__(
        self,
        library: Library,
        game: Game,
        on_save: Callable[[Game], None],
        on_remove: Callable[[Game], None] | None = None,
        on_refresh_artwork: Callable[[Game], None] | None = None,
        on_wine_config: Callable[[Game], None] | None = None,
        parent: Gtk.Widget | None = None,
    ) -> None:
        self._game = game
        form = GameForm(allow_provider=game.source != "local", **_runner_form_args(library))
        form.connect_source_changed(self._on_source_changed)
        form.populate(game)
        super().__init__(
            library,
            title=f"Edit {game.name}",
            form=form,
            parent=parent,
            on_remove=on_remove,
            on_refresh_artwork=on_refresh_artwork,
            on_wine_config=on_wine_config,
        )
        self._save_callback = on_save

    def save(self) -> Game:
        game = self._form.apply_to(self._game)
        self.library.update(game)
        if self._save_callback is not None:
            self._save_callback(game)
        return game

    def _remove_game(self) -> Game:
        return self._game

    def _on_source_changed(self, source: str) -> None:
        """Fetch artwork when the user picks an automatic source and art is absent."""
        self._game.artwork_source = source
        if source == "local" or (self._game.cover and self._game.banner):
            return
        if self._refresh_artwork_callback is not None and self._game.id is not None:
            self._refresh_artwork_callback(self._game)


# Backwards-compatible aliases (the names historically referred to these
# windows even when they were dialogs).
AddGameDialog = AddGameWindow
GameSettingsDialog = GameSettingsWindow


def _runner_form_args(library: Library) -> dict:
    """Build GameForm kwargs listing available runners + the global default."""
    from ..runners import DEFAULT_PROTON_SETTING, list_runners, load_runners_store

    store = load_runners_store(library)
    default = str(library.setting(DEFAULT_PROTON_SETTING, "wine-64") or "wine-64")
    runner_list = [(r.id, r.name) for r in list_runners(store)]
    return {"runner_list": runner_list, "default_runner": default}