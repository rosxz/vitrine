"""Windows for adding and editing a game.

Both the "add a game" window and the per-game settings use the shared
:class:`GameForm`, wiring its browse buttons to GTK's native file chooser.
They are top-level :class:`Gtk.Window` instances so they can be moved around
independently of the main window.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from vitrine.infra.util import expand
from vitrine.services.library import Game, Library
from vitrine.ui.game_form import GameForm, _LabeledEntry

_BROWSE_TITLES: dict[str, str] = {
    "executable": "Choose the game executable",
    "working_dir": "Choose the working directory",
    "cover": "Choose the cover image",
    "banner": "Choose the banner image",
}

#: Browse kinds restricted to images (as opposed to any file for the executable).
_IMAGE_KINDS = {"cover", "banner"}

#: Browse kinds that pick a directory rather than a file.
_FOLDER_KINDS = {"working_dir"}

_FORM_WIDTH = 680
_FORM_HEIGHT = 780


def _parent_window(widget: Gtk.Widget | None) -> Gtk.Window | None:
    if isinstance(widget, Gtk.Window):
        return widget
    if widget is None:
        return None
    root = widget.get_root()
    return root if isinstance(root, Gtk.Window) else None


def _start_dir_for(kind: str, value: str, form: GameForm) -> str | None:
    """The directory a browse dialog should open at, from the current field value.

    Uses the current value if it is (or points into) an existing directory; for
    the executable it falls back to the working directory if that is set.
    """
    resolved = expand(value or "") or ""
    if resolved:
        if os.path.isdir(resolved):
            return resolved
        parent = os.path.dirname(resolved)
        if parent and os.path.isdir(parent):
            return parent
    if kind == "executable":
        working = expand(form.working_dir.text() or "") or ""
        if working and os.path.isdir(working):
            return working
    return None


def _open_picker(
    parent: Gtk.Widget | None,
    kind: str,
    accept: Callable[[str], None],
    start_dir: str | None = None,
) -> None:
    """Open a native file/folder chooser and forward the selected path."""
    folder = kind in _FOLDER_KINDS
    chooser = Gtk.FileDialog(title=_BROWSE_TITLES[kind])
    if kind in _IMAGE_KINDS:
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/avif", "image/bmp"):
            image_filter.add_mime_type(mime)
        chooser.set_default_filter(image_filter)
    if start_dir and os.path.isdir(start_dir):
        try:
            chooser.set_initial_folder(Gio.File.new_for_path(start_dir))
        except Exception:  # noqa: BLE001 - a bad folder must not break Browse
            pass

    def on_selected(dialog: Gtk.FileDialog, result: object) -> None:
        try:
            file = dialog.select_folder_finish(result) if folder else dialog.open_finish(result)
        except (GLib.Error, TypeError):  # cancelled
            return
        if file is not None and file.get_path():
            accept(file.get_path())

    if folder:
        chooser.select_folder(_parent_window(parent), None, on_selected)
    else:
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
        on_pick_artwork: Callable[[Game], None] | None = None,
        on_wine_config: Callable[[Game], None] | None = None,
    ) -> None:
        super().__init__(title=title)
        self.library = library
        self._form = form
        self._save_callback: Callable[[Game], None] | None = None
        self._remove_callback = on_remove
        self._refresh_artwork_callback = on_refresh_artwork
        self._pick_artwork_callback = on_pick_artwork
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
            or self._pick_artwork_callback is not None
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
        if self._pick_artwork_callback is not None:
            pick = Gtk.Button(label="Choose artwork…")
            pick.set_tooltip_text("Pick a tile cover and hero banner from all artwork providers")
            pick.connect("clicked", self._on_pick_artwork)
            footer.append(pick)
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

    def _on_pick_artwork(self, _button: Gtk.Button) -> None:
        if self._pick_artwork_callback is None:
            return
        game = self._remove_game()
        if game is None:
            return
        from vitrine.ui.artwork_picker import ArtworkPickerWindow

        ArtworkPickerWindow(
            self.library,
            game,
            on_chosen=self._on_picker_chosen,
            parent=self,
        ).present()

    def _on_picker_chosen(self, game: Game) -> None:
        """Refresh the form's cover/banner fields with the chosen artwork."""
        self._form.set_browse_result("cover", game.cover or "")
        self._form.set_browse_result("banner", game.banner or "")
        self.library.update(game)
        if self._pick_artwork_callback is not None:
            self._pick_artwork_callback(game)

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
            start_dir = _start_dir_for(kind, entry.text(), self._form)
            _open_picker(self, kind, accept, start_dir=start_dir)

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


class PrefixRecreateWindow(Gtk.Window):
    """Independent confirmation window for deleting and rebuilding a prefix."""

    def __init__(
        self,
        game: Game,
        on_confirm: Callable[[], None],
        parent: Gtk.Widget | None = None,
    ) -> None:
        super().__init__(title=f"Re-create prefix for {game.name}")
        self.add_css_class("vitrine-window")
        self.set_default_size(480, 220)
        parent_window = _parent_window(parent)
        if parent_window is not None:
            self.set_transient_for(parent_window)

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Re-create Wine prefix", subtitle="")
        )
        header.set_show_end_title_buttons(True)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _button: self.close())
        confirm = Gtk.Button(label="Re-create prefix")
        confirm.add_css_class("destructive-action")
        confirm.connect("clicked", lambda _button: self._confirm(on_confirm))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_vexpand(True)
        body.set_hexpand(True)
        body.set_margin_top(24)
        body.set_margin_bottom(24)
        body.set_margin_start(24)
        body.set_margin_end(24)
        message = Gtk.Label(
            label=(
                "This permanently deletes the game's Wine/Proton prefix, "
                "including installed components and settings, then prepares a fresh prefix."
            ),
            wrap=True,
            xalign=0,
        )
        button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        button_box.append(cancel)
        button_box.append(confirm)

        body.append(message)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        body.append(spacer)
        body.append(button_box)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_vexpand(True)
        content.set_hexpand(True)
        content.append(body)
        self.set_titlebar(header)
        self.set_child(content)

    def _confirm(self, on_confirm: Callable[[], None]) -> None:
        self.close()
        on_confirm()


class GameSettingsWindow(_GameWindow):
    """Edit an existing game's metadata and artwork in a movable window."""

    def __init__(
        self,
        library: Library,
        game: Game,
        on_save: Callable[[Game], None],
        on_remove: Callable[[Game], None] | None = None,
        on_refresh_artwork: Callable[[Game], None] | None = None,
        on_pick_artwork: Callable[[Game], None] | None = None,
        on_open_install: Callable[[Game], None] | None = None,
        on_open_prefix: Callable[[Game], None] | None = None,
        on_recreate_prefix: Callable[[Game], None] | None = None,
        on_run_on_prefix: Callable[[Game], None] | None = None,
        on_wine_config: Callable[[Game], None] | None = None,
        parent: Gtk.Widget | None = None,
    ) -> None:
        self._game = game
        form = GameForm(
            allow_provider=game.source != "local",
            **_runner_form_args(library),
            on_open_install=(lambda: on_open_install(game)) if on_open_install else None,
            on_open_prefix=(lambda: on_open_prefix(game)) if on_open_prefix else None,
            on_recreate_prefix=(lambda: on_recreate_prefix(game)) if on_recreate_prefix else None,
            on_run_on_prefix=(lambda: on_run_on_prefix(game)) if on_run_on_prefix else None,
        )
        form.connect_source_changed(self._on_source_changed)
        form.populate(game)
        super().__init__(
            library,
            title=f"Edit {game.name}",
            form=form,
            parent=parent,
            on_remove=on_remove,
            on_refresh_artwork=on_refresh_artwork,
            on_pick_artwork=on_pick_artwork,
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


def choose_add_mode(parent: Gtk.Widget | None, on_choice: Callable[[str], None]) -> None:
    """Ask whether to point at an installed game or install from an installer.

    ``on_choice`` is called with ``"installed"`` or ``"install"`` (never for
    cancel/close).
    """
    dialog = Adw.AlertDialog(
        heading="Add a game",
        body=(
            "Point Vitrine at a game that is already installed on this machine, "
            "or install one from a Windows installer into a new Wine/Proton prefix."
        ),
    )
    dialog.add_response("installed", "Point to an installed game")
    dialog.add_response("install", "Install from an executable")
    dialog.add_response("cancel", "Cancel")
    dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("installed")
    dialog.set_close_response("cancel")

    def _respond(_dialog, response: str) -> None:
        if response in ("installed", "install"):
            on_choice(response)

    dialog.connect("response", _respond)
    dialog.present(_parent_window(parent))


class InstallLocalGameWindow(Gtk.Window):
    """Create a local game by installing it from a Windows installer executable.

    Collects a name, the installer ``.exe`` and the Wine/Proton runner; the
    controller then adds the game and runs the installer in the game's prefix.
    """

    def __init__(
        self,
        library: Library,
        on_install: Callable[[Game], None],
        parent: Gtk.Widget | None = None,
    ) -> None:
        super().__init__(title="Install a game")
        self.library = library
        self._on_install = on_install
        self.add_css_class("vitrine-window")
        self.set_default_size(560, 320)
        parent_window = _parent_window(parent)
        if parent_window is not None:
            self.set_transient_for(parent_window)

        self._name = Gtk.Entry(placeholder_text="Game name")
        self._installer = Gtk.Entry(placeholder_text="Windows installer (.exe)")

        install_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        install_row.append(self._installer)
        self._installer.set_hexpand(True)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", lambda _b: self._choose_installer())
        install_row.append(browse)

        # Runner: default + installed Wine/Proton builds (no "native" -- an
        # installer is always a Windows program).
        self._runner_ids = ["__default__"]
        runner_names = ["Use default"]
        for rid, rname in _runner_form_args(library)["runner_list"]:
            self._runner_ids.append(rid)
            runner_names.append(rname)
        self._runner_row = Gtk.DropDown()
        self._runner_row.set_model(Gtk.StringList.new(runner_names))
        self._runner_row.set_selected(0)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form.set_margin_top(16)
        form.set_margin_bottom(16)
        form.set_margin_start(16)
        form.set_margin_end(16)
        form.append(_labelled("Name", self._name))
        form.append(_labelled("Installer", install_row))
        form.append(_labelled("Wine / Proton", self._runner_row))

        install = Gtk.Button(label="Install", css_classes=["suggested-action"])
        install.connect("clicked", self._on_install_clicked)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Install a game", subtitle=""))
        header.set_show_end_title_buttons(True)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        header.pack_end(install)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(form)
        self.set_titlebar(header)
        self.set_child(content)

    def _choose_installer(self) -> None:
        chooser = Gtk.FileDialog(title="Choose the game's installer")
        installer_filter = Gtk.FileFilter()
        installer_filter.set_name("Windows installers")
        installer_filter.add_pattern("*.exe")
        chooser.set_default_filter(installer_filter)

        def on_selected(dialog: Gtk.FileDialog, result: object) -> None:
            try:
                file = dialog.open_finish(result)
            except (GLib.Error, TypeError):
                return
            if file is not None and file.get_path():
                self._installer.set_text(file.get_path())

        chooser.open(self, None, on_selected)

    def _on_install_clicked(self, _button: Gtk.Button) -> None:
        name = self._name.get_text().strip()
        installer = self._installer.get_text().strip()
        if not name or not installer:
            return
        runner_id = self._runner_ids[self._runner_row.get_selected()]
        config: dict = {"installer": installer}
        if runner_id not in ("__default__", "native"):
            config["runner"] = runner_id
        game = Game(
            name=name,
            runner="wine",
            executable=None,
            config=config,
            source="local",
            installed=False,
        )
        self._on_install(game)
        self.close()


LocalInstallDialog = InstallLocalGameWindow
InstallLocalDialog = InstallLocalGameWindow


def _labelled(label: str, widget: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    caption = Gtk.Label(label=label, halign=Gtk.Align.START)
    caption.add_css_class("caption")
    box.append(caption)
    box.append(widget)
    return box


def _runner_form_args(library: Library) -> dict:
    """Build GameForm kwargs listing available runners + the global default."""
    from vitrine.services.runners import DEFAULT_PROTON_SETTING, list_runners, load_runners_store

    store = load_runners_store(library)
    default = str(library.setting(DEFAULT_PROTON_SETTING, "wine-64") or "wine-64")
    runner_list = [(r.id, r.name) for r in list_runners(store)]
    return {"runner_list": runner_list, "default_runner": default}