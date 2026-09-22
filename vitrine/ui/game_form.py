"""A reusable game editor form.

Both the "add a game" dialog and the per-game settings view build their body
from this widget, so the fields, layout and artwork pickers live in exactly one
place. Editing populates the fields from a game; saving produces a :class:`Game`
(or applies values onto an existing one).

Artwork and executable fields expose a browse button. When clicked the form
calls the host-provided ``on_browse(kind)`` callback with a field key
(``"executable"``, ``"cover"`` or ``"banner"``); the host is expected to open a
native file chooser and route the result back through
:meth:`GameForm.set_browse_result`.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Gtk

from ..library import Game
from ..util import expand

#: Browse button field keys.
BROWSE_FIELDS = ("executable", "cover", "banner")


class _LabeledEntry(Gtk.Box):
    """A labelled, optional-browse text entry."""

    def __init__(self, title: str, browse: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        label = Gtk.Label(label=title, halign=Gtk.Align.START)
        label.add_css_class("vitrine-form-label")
        label.set_margin_start(2)
        self.append(label)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry(hexpand=True)
        row.append(self.entry)

        self._browse: Gtk.Button | None = None
        if browse:
            self._browse = Gtk.Button(label="Browse…")
            self._browse.set_valign(Gtk.Align.CENTER)
            row.append(self._browse)
        self.append(row)

    def on_browse(self, callback: Callable[[], None]) -> None:
        if self._browse is not None:
            self._browse.connect("clicked", lambda _btn: callback())

    def text(self) -> str:
        return self.entry.get_text().strip()

    def value(self) -> str | None:
        return self.text() or None

    def set(self, value: str | None) -> None:
        self.entry.set_text(value or "")


class GameForm(Gtk.Box):
    """Shared editor for a game's metadata and artwork.

    ``on_browse(kind, entry)`` is invoked when a browse button is pressed. The
    host should open a native file chooser and route the result back through
    :meth:`set_browse_result`.
    """

    def __init__(
        self,
        on_browse: Callable[[str, _LabeledEntry], None] | None = None,
        *,
        allow_provider: bool = True,
        runner_list: list[tuple[str, str]] | None = None,
        default_runner: str = "wine-64",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._on_browse = on_browse or (lambda _kind, _entry: None)
        self._on_source_changed: Callable[[str], None] | None = None
        self._allow_provider = allow_provider
        self._loading = False
        self._runner_list = runner_list or []
        self._default_runner_id = default_runner

        self.name = _LabeledEntry("Name")
        self.executable = _LabeledEntry("Executable", browse=True)
        self.arguments = _LabeledEntry("Arguments")
        self.working_dir = _LabeledEntry("Working directory")
        self.prefix = _LabeledEntry("Wine prefix (optional)")
        self.cover = _LabeledEntry("Cover image (portrait)", browse=True)
        self.banner = _LabeledEntry("Banner image (wide hero)", browse=True)

        self.lutris_slug = _LabeledEntry("Lutris slug (defaults to game name)")

        # Wine/Proton runner selector, with an explicit "use default" choice.
        runner_ids = ["__default__"]
        runner_names = ["Use default"]
        for rid, rname in self._runner_list:
            runner_ids.append(rid)
            runner_names.append(rname)
        self._runner_option_ids = runner_ids
        runner_label = Gtk.Label(label="Wine / Proton", halign=Gtk.Align.START)
        runner_label.add_css_class("caption")
        self.runner_row = Gtk.DropDown()
        self.runner_row.set_model(Gtk.StringList.new(runner_names))
        self.runner_row.set_selected(0)

        art_sources = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._source_label = Gtk.Label(label="Artwork:", halign=Gtk.Align.START)
        art_sources.append(self._source_label)
        self._artwork_source = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._source_buttons: dict[str, Gtk.CheckButton] = {}
        group = None
        sources = (("local", "Local"), ("lutris", "Lutris"))
        if self._allow_provider:
            sources = (("local", "Local"), ("provider", "Provider"), ("lutris", "Lutris"))
        for src, label in sources:
            if group is None:
                btn = Gtk.CheckButton(label=label)
                group = btn  # subsequent buttons join this radio group.
            else:
                btn = Gtk.CheckButton(label=label, group=group)
            btn.connect("toggled", self._on_source_toggled, src)
            self._artwork_source.append(btn)
            self._source_buttons[src] = btn
        self._source_buttons["lutris"].set_active(True)
        art_sources.append(self._artwork_source)

        for entry in (
            self.name,
            self.executable,
            self.arguments,
            self.working_dir,
            self.prefix,
            self.cover,
            self.banner,
            self.lutris_slug,
        ):
            self.append(entry)
        self.append(art_sources)
        self.append(runner_label)
        self.append(self.runner_row)

        # Opt-in gamescope (nested display/GPU session) on the host compositor.
        self.gamescope_row = Gtk.CheckButton(
            label="Gamescope", active=False, halign=Gtk.Align.START
        )
        self.gamescope_row.set_tooltip_text(
            "Run this game inside a gamescope window (virtualized display). "
            "Recommended on Wayland for many Windows games. Disables MangoHud."
        )
        self.append(self.gamescope_row)

        self._fields: dict[str, _LabeledEntry] = {
            "executable": self.executable,
            "cover": self.cover,
            "banner": self.banner,
        }
        for kind, entry in self._fields.items():
            entry.on_browse(lambda k=kind, e=entry: self._on_browse(k, e))

    def _on_source_toggled(self, btn: Gtk.CheckButton, src: str) -> None:
        if self._loading or not btn.get_active():
            return
        if self._on_source_changed is not None:
            self._on_source_changed(src)

    def connect_source_changed(self, handler: Callable[[str], None]) -> None:
        """Subscribe to artwork-source changes (only active selections fire)."""
        self._on_source_changed = handler

    def connect_browse(self, kind: str, handler: Callable[[_LabeledEntry], None]) -> None:
        """Bind a host-provided file-picker to one browse button."""
        entry = self._fields.get(kind)
        if entry is not None:
            entry.on_browse(lambda e=entry: handler(e))

    def set_browse_result(self, kind: str, path: str) -> None:
        """Apply a file picker result to the given field."""
        entry = self._fields.get(kind)
        if entry is not None:
            entry.set(str(expand(path) or ""))

    def populate(self, game: Game) -> None:
        self.name.set(game.name)
        self.executable.set(expand(game.executable))
        self.arguments.set(game.arguments)
        self.working_dir.set(expand(game.working_dir))
        self.prefix.set(expand(game.prefix))
        self.cover.set(expand(game.cover))
        self.banner.set(expand(game.banner))
        self.lutris_slug.set(game.lutris_slug)
        self.set_artwork_source(game.artwork_source)
        self.set_runner(game.config.get("runner"))
        self.gamescope_row.set_active(bool(game.config.get("gamescope", False)))

    def set_runner(self, runner_id: str | None) -> None:
        """Select the per-game runner override, or the default if unset."""
        if not runner_id or runner_id in ("__default__", "", self._default_runner_id):
            self.runner_row.set_selected(0)
            return
        ids = self._runner_option_ids
        if runner_id in ids:
            self.runner_row.set_selected(ids.index(runner_id))

    def runner(self) -> str | None:
        """The per-game runner override id, or ``None`` to use the default."""
        selected = self.runner_row.get_selected()
        option = self._runner_option_ids[selected] if 0 <= selected < len(self._runner_option_ids) else "__default__"
        return None if option == "__default__" else option

    def set_artwork_source(self, source: str) -> None:
        source = source or "lutris"
        if source not in self._source_buttons:
            source = "lutris"
        self._loading = True
        try:
            self._source_buttons[source].set_active(True)
        finally:
            self._loading = False

    def artwork_source(self) -> str:
        for src, btn in self._source_buttons.items():
            if btn.get_active():
                return src
        return "lutris"

    def validate(self) -> str | None:
        if not self.name.text():
            return "A name is required."
        return None

    def gamescope(self) -> bool:
        """Whether the per-game gamescope flag is enabled."""
        return bool(self.gamescope_row.get_active())

    def build_game(self) -> Game:
        v = self._values()
        runner = self.runner()
        config: dict = {"gamescope": self.gamescope()}
        if runner:
            config["runner"] = runner
        return Game(
            name=v["name"],
            runner="wine",
            executable=v["executable"],
            arguments=v["arguments"],
            working_dir=v["working_dir"],
            prefix=v["prefix"],
            cover=v["cover"],
            banner=v["banner"],
            artwork_source=v["artwork_source"],
            lutris_slug=v["lutris_slug"],
            config=config,
            source="local",
            installed=True,
        )

    def apply_to(self, game: Game) -> Game:
        v = self._values()
        game.name = v["name"]
        game.executable = v["executable"]
        game.arguments = v["arguments"]
        game.working_dir = v["working_dir"]
        game.prefix = v["prefix"]
        game.cover = v["cover"]
        game.banner = v["banner"]
        game.artwork_source = v["artwork_source"]
        game.lutris_slug = v["lutris_slug"]
        runner = self.runner()
        if runner:
            game.config["runner"] = runner
        else:
            game.config.pop("runner", None)
        game.config["gamescope"] = self.gamescope()
        return game

    def _values(self) -> dict:
        return {
            "name": self.name.text(),
            "executable": self.executable.value(),
            "arguments": self.arguments.value(),
            "working_dir": self.working_dir.value(),
            "prefix": self.prefix.value(),
            "cover": self.cover.value(),
            "banner": self.banner.value(),
            "artwork_source": self.artwork_source(),
            "lutris_slug": (self.lutris_slug.value() or "").strip(),
        }