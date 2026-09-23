"""Per-game artwork picker window.

Shows every candidate tile (portrait cover) and hero (wide banner) gathered from
the configured artwork providers as scaled thumbnails, grouped by provider. The
user picks exactly one tile and one hero; each slot is mutually exclusive (a
radio group) across all providers. Applying downloads the chosen full-resolution
candidates into Vitrine's cache and persists them onto the game.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Callable

from gi.repository import Adw, Gdk, GdkPixbuf, Gtk

from .. import artwork
from ..artwork_providers import net
from ..artwork_providers.base import Art, provider_label

logger = logging.getLogger(__name__)

#: Per-provider-per-slot cap so one provider (e.g. many SGDB grids) never crowds
#: another provider out of the picker.
_PER_SLOT_CAP = 8
#: Tile preview size (portrait 2:3).
_TILE_W, _TILE_H = 132, 198
#: Hero preview size (wide banner, ~16:7).
_HERO_W, _HERO_H = 320, 140
#: How many thumbnails to fetch at once.
_THUMB_WORKERS = 4


def _preview_url(art: Art) -> str:
    """Pick the sharpest preview URL for a slot.

    Heroes are shown large, and provider ``thumb`` sizes are often tiny (upscaled
    → blurry), so use the full-resolution URL for heroes (only ever downscaled).
    Tiles stay on the small ``thumb`` (they're portraits ~132px wide anyway).
    """
    return art.url if art.slot == "hero" else (art.thumb or art.url)


def _texture_from_bytes(data: bytes, max_width: int) -> Gdk.Texture | None:
    """Turn raw image bytes into a Gdk.Texture preview (≤ ``max_width`` px wide).

    ``max_width`` matches the preview slot so a higher-resolution source is only
    ever *downscaled*, which keeps heroes sharp instead of upscaling a tiny thumb.
    """
    from gi.repository import GLib
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail((max_width, 9999), Image.LANCZOS)
        width, height = img.size
        rgba = img.tobytes("raw", "RGBA")
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(rgba),
            GdkPixbuf.Colorspace.RGB,
            True,
            8,
            int(width),
            int(height),
            int(width) * 4,
        )
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:  # noqa: BLE001 - a corrupt thumb must not crash the picker
        logger.debug("Could not decode art preview", exc_info=True)
        return None


class ArtworkPickerWindow(Gtk.Window):
    """Movable window to pick a tile+hero pair from all providers."""

    def __init__(
        self,
        library,
        game,
        on_chosen: Callable[[object], None] | None = None,
        parent: Gtk.Window | None = None,
    ) -> None:
        super().__init__(title=f"Choose artwork — {getattr(game, 'name', '')}")
        self.library = library
        self.game = game
        self._on_chosen = on_chosen
        # Snapshot credentials + priority on the main thread: the worker thread
        # that gathers candidates must not touch the library's sqlite connection.
        self._ctx = artwork.load_context(library)
        self.add_css_class("vitrine-window")
        self.set_default_size(880, 640)
        if parent is not None:
            self.set_transient_for(parent)

        self._tile_group: Gtk.CheckButton | None = None
        self._hero_group: Gtk.CheckButton | None = None
        self._tile_checks: dict[str, Gtk.CheckButton] = {}
        self._hero_checks: dict[str, Gtk.CheckButton] = {}
        self._pictures: dict[str, Gtk.Picture] = {}

        # One tab per artwork provider.
        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_vexpand(True)
        empty_scroll = Gtk.ScrolledWindow()
        empty_scroll.set_child(_status_label("Gathering artwork…"))
        self._notebook.append_page(empty_scroll, Gtk.Label(label="Loading"))

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=self.get_title(), subtitle=""))
        header.set_show_end_title_buttons(True)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)
        self._apply = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        self._apply.set_sensitive(False)
        self._apply.connect("clicked", self._on_apply)
        header.pack_end(self._apply)

        self.set_titlebar(header)
        self.set_child(self._notebook)

        threading.Thread(target=self._load, daemon=True).start()

    # -- loading ---------------------------------------------------------------

    def _load(self) -> None:
        self._cards: list[tuple[str, str, Art]] = []
        try:
            candidates = artwork.provider_candidates(self.library, self.game, self._ctx)
        except Exception:  # noqa: BLE001
            logger.exception("gathering artwork candidates for %s", getattr(self.game, "name", ""))
            candidates = None
        seen: set[str] = set()
        if candidates is not None:
            # Cap per provider per slot so no single provider floods the picker.
            for pid in candidates.by_provider:
                for slot in ("tile", "hero"):
                    for art in candidates.by_provider.get(pid, {}).get(slot, [])[:_PER_SLOT_CAP]:
                        if art.url in seen:
                            continue
                        seen.add(art.url)
                        self._cards.append((pid, slot, art))
        logger.debug("artwork picker gathered %d candidates for %s", len(self._cards), getattr(self.game, "name", ""))
        if not self._cards:
            GLib_idle(self._show_empty)
            return
        # Build the UI immediately so the user sees the candidate list right
        # away; thumbnails stream in concurrently from a small pool.
        GLib_idle(self._build, self._cards)
        self._fetch_thumbs()

    def _fetch_thumbs(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(slot: str, art: Art) -> None:
            data = net.get_bytes(_preview_url(art))
            if data:
                GLib_idle(lambda: self._set_thumb(art.key(), data, slot))

        jobs = [(slot, art) for _pid, slot, art in self._cards]
        with ThreadPoolExecutor(max_workers=_THUMB_WORKERS) as pool:
            for slot, art in jobs:
                pool.submit(_fetch, slot, art)

    def _set_thumb(self, key: str, data: bytes, slot: str) -> None:
        picture = self._pictures.get(key)
        if picture is None:
            return
        max_width = _HERO_W if slot == "hero" else _TILE_W
        texture = _texture_from_bytes(data, max_width)
        if texture is not None:
            picture.set_paintable(texture)

    def _show_empty(self) -> None:
        self._clear_pages()
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(_status_label(
            "No artwork candidates found. Configure an IGDB/SteamGridDB key in "
            "Settings → Appearance and try again."
        ))
        self._notebook.append_page(scroll, Gtk.Label(label="No artwork"))

    def _clear_pages(self) -> None:
        while self._notebook.get_n_pages() > 0:
            self._notebook.remove_page(0)

    def _build(self, cards: list[tuple[str, str, Art]]) -> None:
        self._clear_pages()
        # One tab per provider; within a tab, tile covers then hero banners.
        pages: dict[str, Gtk.Box] = {}
        flows: dict[tuple[str, str], Gtk.FlowBox] = {}
        for pid, slot, art in cards:
            if pid not in pages:
                page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
                page_box.set_margin_top(12)
                page_box.set_margin_bottom(12)
                page_box.set_margin_start(16)
                page_box.set_margin_end(16)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                scroll.set_vexpand(True)
                scroll.set_child(page_box)
                pages[pid] = page_box
                self._notebook.append_page(scroll, Gtk.Label(label=provider_label(pid)))
            key = (pid, slot)
            if key not in flows:
                flows[key] = Gtk.FlowBox()
                flows[key].set_column_spacing(12)
                flows[key].set_row_spacing(12)
                flows[key].set_selection_mode(Gtk.SelectionMode.NONE)
                pages[pid].append(_slot_label(
                    "Tile covers — pick one (mutually exclusive)" if slot == "tile"
                    else "Hero banners — pick one (mutually exclusive)"
                ))
                pages[pid].append(flows[key])
            flows[key].append(self._card(pid, slot, art))

    def _card(self, pid: str, slot: str, art: Art) -> Gtk.Widget:
        w, h = (_TILE_W, _TILE_H) if slot == "tile" else (_HERO_W, _HERO_H)
        picture = Gtk.Picture()
        picture.set_size_request(w, h)
        picture.set_overflow(Gtk.Overflow.HIDDEN)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        self._pictures[art.key()] = picture

        frame = Gtk.Frame()
        frame.set_child(picture)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(frame)

        label_text = art.label or provider_label(pid)
        if slot == "tile":
            if self._tile_group is None:
                self._tile_group = Gtk.CheckButton(label=label_text)
                check = self._tile_group
            else:
                check = Gtk.CheckButton(label=label_text, group=self._tile_group)
            self._tile_checks[art.key()] = check
        else:
            if self._hero_group is None:
                self._hero_group = Gtk.CheckButton(label=label_text)
                check = self._hero_group
            else:
                check = Gtk.CheckButton(label=label_text, group=self._hero_group)
            self._hero_checks[art.key()] = check
        check.connect("toggled", self._on_toggled, slot)
        box.append(check)
        return box

    def _on_toggled(self, _check: Gtk.CheckButton, _slot: str) -> None:
        self._apply.set_sensitive(
            self._selected("tile") is not None or self._selected("hero") is not None
        )

    def _selected(self, slot: str) -> Art | None:
        checks = self._tile_checks if slot == "tile" else self._hero_checks
        for key, check in checks.items():
            if check.get_active():
                parts = key.split("|", 2)
                if len(parts) == 3:
                    return Art(provider=parts[0], slot=slot, url=parts[2])
        return None

    def _on_apply(self, _button: Gtk.Button) -> None:
        tile = self._selected("tile")
        hero = self._selected("hero")
        pair = ((tile.url if tile else ""), (hero.url if hero else ""))
        if not pair[0] and not pair[1]:
            return
        try:
            dims = (self._ctx.get("tile_dim"), self._ctx.get("hero_dim"))
            artwork.fetch(pair, self.game, force=True, dims=dims)
        except Exception:  # noqa: BLE001
            logger.exception("downloading chosen artwork")
        if self.game.id is not None:
            self.library.update(self.game)
        if self._on_chosen is not None:
            self._on_chosen(self.game)
        self.close()


def _slot_label(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text, halign=Gtk.Align.START)
    label.add_css_class("caption")
    return label


def _status_label(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text, wrap=True, halign=Gtk.Align.START)
    label.add_css_class("dim-label")
    label.set_selectable(True)
    return label


def GLib_idle(fn: Callable[[], None], *args) -> None:
    """Schedule ``fn(*args)`` on the GTK main loop and drop it after one run."""
    from gi.repository import GLib

    if args:
        GLib.idle_add(lambda: fn(*args))
    else:
        GLib.idle_add(fn)


ArtworkPicker = ArtworkPickerWindow