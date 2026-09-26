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

#: Common locales offered as completion presets for the per-game Locale field.
_COMMON_LOCALES = (
    "en_US.UTF-8",
    "ja_JP.UTF-8",
    "zh_CN.UTF-8",
    "ko_KR.UTF-8",
    "fr_FR.UTF-8",
    "de_DE.UTF-8",
    "es_ES.UTF-8",
    "pt_BR.UTF-8",
    "ru_RU.UTF-8",
    "it_IT.UTF-8",
)


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
        on_open_install: Callable[[], None] | None = None,
        on_open_prefix: Callable[[], None] | None = None,
        on_recreate_prefix: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._on_browse = on_browse or (lambda _kind, _entry: None)
        self._on_source_changed: Callable[[str], None] | None = None
        self._allow_provider = allow_provider
        self._loading = False
        self._runner_list = runner_list or []
        self._default_runner_id = default_runner
        self._on_open_install = on_open_install
        self._on_open_prefix = on_open_prefix
        self._on_recreate_prefix = on_recreate_prefix

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
        sources = (("auto", "Auto"), ("local", "Local"), ("lutris", "Lutris"))
        if self._allow_provider:
            sources = (("auto", "Auto"), ("local", "Local"), ("provider", "Provider"), ("lutris", "Lutris"))
        for src, label in sources:
            if group is None:
                btn = Gtk.CheckButton(label=label)
                group = btn  # subsequent buttons join this radio group.
            else:
                btn = Gtk.CheckButton(label=label, group=group)
            btn.connect("toggled", self._on_source_toggled, src)
            self._artwork_source.append(btn)
            self._source_buttons[src] = btn
        self._source_buttons["auto"].set_active(True)
        art_sources.append(self._artwork_source)

        # Opt-in gamescope (nested display/GPU session) on the host compositor.
        self.gamescope_row = Gtk.CheckButton(
            label="Gamescope", active=False, halign=Gtk.Align.START
        )
        self.gamescope_row.set_tooltip_text(
            "Run this game inside a gamescope window (virtualized display). "
            "Recommended on Wayland for many Windows games. Disables MangoHud."
        )
        self.gamescope_game_res = Gtk.Entry(placeholder_text="e.g. 1280x720", hexpand=True)
        self.gamescope_game_res.set_tooltip_text("Resolution rendered by the game")
        self.gamescope_output_res = Gtk.Entry(placeholder_text="e.g. 1920x1080", hexpand=True)
        self.gamescope_output_res.set_tooltip_text("Resolution presented by gamescope")
        self.gamescope_mode = Gtk.DropDown()
        self.gamescope_mode.set_model(
            Gtk.StringList.new(["Fullscreen", "Borderless", "Windowed"])
        )
        self.gamescope_mode.set_selected(0)
        self.gamescope_relative_mouse = Gtk.CheckButton(label="Relative mouse / grab cursor")
        self.gamescope_relative_mouse.set_tooltip_text(
            "Capture the pointer for games that lose mouse input"
        )
        self.gamescope_fps = Gtk.Entry(placeholder_text="e.g. 60", hexpand=True)
        self.gamescope_fps.set_tooltip_text("Gamescope frame-rate limit")

        # Per-game DXVK toggle (on by default). Off forces Wine's built-in
        # Direct3D translators instead of the Vulkan-based DXVK renderer.
        self.dxvk_row = Gtk.CheckButton(
            label="DXVK", active=True, halign=Gtk.Align.START
        )
        self.dxvk_row.set_tooltip_text(
            "Use DXVK to translate Direct3D to Vulkan. Disable for games that "
            "misbehave with DXVK (they'll use Wine's built-in D3D instead)."
        )

        # Performance / anti-cheat toggles (Lutris-style).
        self.esync_row = Gtk.CheckButton(label="Esync", active=True, halign=Gtk.Align.START)
        self.esync_row.set_tooltip_text(
            "Enable eventfd-based synchronization (esync) for better multi-core performance."
        )
        self.fsync_row = Gtk.CheckButton(label="Fsync", active=True, halign=Gtk.Align.START)
        self.fsync_row.set_tooltip_text(
            "Enable futex-based synchronization (fsync). Requires kernel 5.16+."
        )
        self.fsr_row = Gtk.CheckButton(label="FSR", active=True, halign=Gtk.Align.START)
        self.fsr_row.set_tooltip_text(
            "AMD FidelityFX Super Resolution upscaling (with gamescope). "
            "Run the game at a lower resolution and FSR upscales it."
        )
        self.eac_row = Gtk.CheckButton(label="EasyAntiCheat", active=True, halign=Gtk.Align.START)
        self.eac_row.set_tooltip_text(
            "Enable Easy Anti-Cheat support (uses Proton's EAC runtime when available)."
        )

        self._fields: dict[str, _LabeledEntry] = {
            "executable": self.executable,
            "cover": self.cover,
            "banner": self.banner,
        }
        for kind, entry in self._fields.items():
            entry.on_browse(lambda k=kind, e=entry: self._on_browse(k, e))

        notebook = Gtk.Notebook()
        notebook.set_scrollable(True)

        general = self._tab()
        for entry in (self.name, self.executable, self.arguments, self.working_dir):
            general.append(entry)
        # Reveal the game's on-disk directories in the file manager (edit-only).
        if self._on_open_install or self._on_open_prefix or self._on_recreate_prefix:
            dir_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dir_buttons.set_margin_top(4)
            if self._on_open_install:
                btn = Gtk.Button(label="Open installation directory…")
                btn.connect("clicked", lambda _b: self._on_open_install())
                dir_buttons.append(btn)
            if self._on_open_prefix:
                btn = Gtk.Button(label="Open prefix directory…")
                btn.connect("clicked", lambda _b: self._on_open_prefix())
                dir_buttons.append(btn)
            if self._on_recreate_prefix:
                btn = Gtk.Button(label="Re-create prefix")
                btn.add_css_class("destructive-action")
                btn.set_tooltip_text("Delete this prefix and prepare a fresh one")
                btn.connect("clicked", lambda _b: self._on_recreate_prefix())
                dir_buttons.append(btn)
            general.append(dir_buttons)

        launcher = self._tab()
        launcher.append(self.prefix)
        launcher.append(runner_label)
        launcher.append(self.runner_row)
        launcher.append(self.gamescope_row)
        gamescope_label = Gtk.Label(label="Gamescope options", halign=Gtk.Align.START)
        gamescope_label.add_css_class("caption")
        launcher.append(gamescope_label)
        launcher.append(self._gamescope_entry_row("Game resolution", self.gamescope_game_res))
        launcher.append(self._gamescope_entry_row("Output resolution", self.gamescope_output_res))
        launcher.append(self._gamescope_labeled_row("Window mode", self.gamescope_mode))
        launcher.append(self.gamescope_relative_mouse)
        launcher.append(self._gamescope_entry_row("FPS limit", self.gamescope_fps))
        launcher.append(self.dxvk_row)
        launcher.append(self.esync_row)
        launcher.append(self.fsync_row)
        launcher.append(self.fsr_row)
        launcher.append(self.eac_row)

        appearance = self._tab()
        appearance.append(self.cover)
        appearance.append(self.banner)
        appearance.append(art_sources)

        details = self._tab()
        details.append(self.lutris_slug)

        environment = self._build_environment_tab()

        for tab, title in (
            (general, "General"),
            (launcher, "Launcher"),
            (appearance, "Appearance"),
            (environment, "Environment"),
            (details, "Details"),
        ):
            notebook.append_page(tab, Gtk.Label(label=title))

        self.append(notebook)

    @staticmethod
    def _tab() -> Gtk.Box:
        """A vertical container for a single tab's fields."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        return box

    @staticmethod
    def _gamescope_entry_row(label: str, entry: Gtk.Entry) -> Gtk.Box:
        return GameForm._gamescope_labeled_row(label, entry)

    @staticmethod
    def _gamescope_labeled_row(label: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        caption = Gtk.Label(label=label, xalign=0, hexpand=True)
        caption.add_css_class("caption")
        row.append(caption)
        row.append(widget)
        return row

    def _build_environment_tab(self) -> Gtk.Widget:
        """Per-game locale + environment-variable list (Lutris-style)."""
        tab = self._tab()

        locale_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        locale_label = Gtk.Label(label="Locale (LANG / LC_ALL)", halign=Gtk.Align.START)
        locale_label.add_css_class("caption")
        self.locale_entry = Gtk.Entry()
        self.locale_entry.set_placeholder_text("e.g. en_US.UTF-8 — pick a preset or type")
        completion = Gtk.EntryCompletion()
        store = Gtk.ListStore(str)
        for locale in _COMMON_LOCALES:
            store.append([locale])
        completion.set_model(store)
        completion.set_text_column(0)
        self.locale_entry.set_completion(completion)
        locale_group.append(locale_label)
        locale_group.append(self.locale_entry)
        tab.append(locale_group)

        env_label = Gtk.Label(label="Environment variables", halign=Gtk.Align.START)
        env_label.add_css_class("caption")
        env_hint = Gtk.Label(
            label="Each variable is exported to the game's process (e.g. DXVK_HUD=fps).",
            halign=Gtk.Align.START, wrap=True, xalign=0.0,
        )
        env_hint.add_css_class("dim-label")
        tab.append(env_label)
        tab.append(env_hint)

        self._env_list = Gtk.ListBox()
        self._env_list.set_selection_mode(Gtk.SelectionMode.NONE)
        tab.append(self._env_list)

        add_button = Gtk.Button(label="Add variable")
        add_button.add_css_class("suggested-action")
        add_button.set_halign(Gtk.Align.START)
        add_button.connect("clicked", lambda _b: self._add_env_row())
        tab.append(add_button)
        return tab

    def _add_env_row(self, key: str = "", value: str = "") -> None:
        """Append one key/value environment-variable row to the list."""
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        key_entry = Gtk.Entry()
        key_entry.set_placeholder_text("VARIABLE")
        key_entry.set_hexpand(True)
        key_entry.set_text(key)
        value_entry = Gtk.Entry()
        value_entry.set_placeholder_text("value")
        value_entry.set_hexpand(True)
        value_entry.set_text(value)
        remove_button = Gtk.Button(icon_name="edit-delete-symbolic")
        remove_button.add_css_class("flat")
        remove_button.set_tooltip_text("Remove variable")
        remove_button.connect("clicked", lambda _b, r=row: self._env_list.remove(r))
        box.append(key_entry)
        box.append(value_entry)
        box.append(remove_button)
        row.set_child(box)
        self._env_list.append(row)

    def _env_values(self) -> dict[str, str]:
        """Gather the (non-blank) environment variables as a dict."""
        result: dict[str, str] = {}
        child = self._env_list.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBoxRow):
                first = child.get_child()
                if isinstance(first, Gtk.Box):
                    key_widget = first.get_first_child()
                    value_widget = key_widget.get_next_sibling() if key_widget is not None else None
                    key = key_widget.get_text().strip() if isinstance(key_widget, Gtk.Entry) else ""
                    value = value_widget.get_text() if isinstance(value_widget, Gtk.Entry) else ""
                    if key:
                        result[key] = value
            child = child.get_next_sibling()
        return result

    def _set_env(self, env: dict[str, str] | None) -> None:
        while (child := self._env_list.get_first_child()) is not None:
            self._env_list.remove(child)
        for key, value in (env or {}).items():
            self._add_env_row(key, value)

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
        self.gamescope_game_res.set_text(str(game.config.get("gamescope_game_res") or ""))
        self.gamescope_output_res.set_text(str(game.config.get("gamescope_output_res") or ""))
        self.gamescope_mode.set_selected({"-f": 0, "-b": 1, "windowed": 2}.get(game.config.get("gamescope_window_mode"), 0))
        self.gamescope_relative_mouse.set_active(bool(game.config.get("gamescope_relative_mouse", False)))
        self.gamescope_fps.set_text(str(game.config.get("gamescope_fps_limiter") or ""))
        self.locale_entry.set_text(game.config.get("locale") or "")
        self._set_env(game.config.get("env") or {})
        self.dxvk_row.set_active(bool(game.config.get("dxvk", True)))
        self.esync_row.set_active(bool(game.config.get("esync", True)))
        self.fsync_row.set_active(bool(game.config.get("fsync", True)))
        self.fsr_row.set_active(bool(game.config.get("fsr", True)))
        self.eac_row.set_active(bool(game.config.get("eac", True)))

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
        source = source or "auto"
        if source not in self._source_buttons:
            source = "auto"
        self._loading = True
        try:
            self._source_buttons[source].set_active(True)
        finally:
            self._loading = False

    def artwork_source(self) -> str:
        for src, btn in self._source_buttons.items():
            if btn.get_active():
                return src
        return "auto"

    def validate(self) -> str | None:
        if not self.name.text():
            return "A name is required."
        return None

    def gamescope(self) -> bool:
        """Whether the per-game gamescope flag is enabled."""
        return bool(self.gamescope_row.get_active())

    def gamescope_window_mode(self) -> str:
        return ["-f", "-b", "windowed"][self.gamescope_mode.get_selected()]

    def gamescope_game_resolution(self) -> str:
        return self.gamescope_game_res.get_text().strip()

    def gamescope_output_resolution(self) -> str:
        return self.gamescope_output_res.get_text().strip()

    def gamescope_fps_limit(self) -> str:
        return self.gamescope_fps.get_text().strip()

    def dxvk(self) -> bool:
        """Whether the per-game DXVK toggle is enabled (default on)."""
        return bool(self.dxvk_row.get_active())

    def esync(self) -> bool:
        return bool(self.esync_row.get_active())

    def fsync(self) -> bool:
        return bool(self.fsync_row.get_active())

    def fsr(self) -> bool:
        return bool(self.fsr_row.get_active())

    def eac(self) -> bool:
        return bool(self.eac_row.get_active())

    def build_game(self) -> Game:
        v = self._values()
        runner = self.runner()
        config: dict = {
            "gamescope": self.gamescope(),
            "gamescope_window_mode": self.gamescope_window_mode(),
            "gamescope_game_res": self.gamescope_game_resolution(),
            "gamescope_output_res": self.gamescope_output_resolution(),
            "gamescope_relative_mouse": self.gamescope_relative_mouse.get_active(),
            "gamescope_fps_limiter": self.gamescope_fps_limit(),
            "dxvk": self.dxvk(),
            "esync": self.esync(),
            "fsync": self.fsync(),
            "fsr": self.fsr(),
            "eac": self.eac(),
            "env": self._env_values(),
        }
        locale = self.locale_entry.get_text().strip()
        if locale:
            config["locale"] = locale
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
        game.config["gamescope_window_mode"] = self.gamescope_window_mode()
        game.config["gamescope_game_res"] = self.gamescope_game_resolution()
        game.config["gamescope_output_res"] = self.gamescope_output_resolution()
        game.config["gamescope_relative_mouse"] = self.gamescope_relative_mouse.get_active()
        game.config["gamescope_fps_limiter"] = self.gamescope_fps_limit()
        game.config["dxvk"] = self.dxvk()
        game.config["esync"] = self.esync()
        game.config["fsync"] = self.fsync()
        game.config["fsr"] = self.fsr()
        game.config["eac"] = self.eac()
        game.config["env"] = self._env_values()
        locale = self.locale_entry.get_text().strip()
        if locale:
            game.config["locale"] = locale
        else:
            game.config.pop("locale", None)
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