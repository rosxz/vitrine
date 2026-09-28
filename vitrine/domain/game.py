"""The game domain model (pure data; no database or GUI).

``Game`` is the mutable library entry Vitrine operates on, and :data:`DEFAULT_CONFIG`
the merged per-game launcher configuration defaults. Both are framework-free so
the domain layer imports nothing from the database or presentation layers.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "graphics": "x11",  # "x11" or "wayland"
    "gamemode": False,
    "mangohud": False,
    "gamescope": False,
    "gamescope_window_mode": "-f",
    "gamescope_game_res": "",
    "gamescope_output_res": "",
    "gamescope_fps_limiter": "",
    "gamescope_flags": "",
    "gamescope_fsr_sharpness": "",
    "gamescope_force_grab_cursor": True,
    "gamescope_hdr": False,
    #: Runner id (a preset like ``wine-64`` or a discovered Wine/Proton build).
    "runner": "wine-64",
    "wine_binary": None,
    "dll_overrides": "",
    "dxvk": True,
    "vkd3d": True,
    #: Install the bundled DirectX 9/10/11 runtime DLLs (d3dx9_43, ...) into the
    #: prefix so old games that need them launch under Wine/Proton.
    "d3d_extras": True,
    "esync": True,
    "fsync": True,
    #: AMD FidelityFX Super Resolution (FSR) upscaling (via gamescope / wine-fsr).
    "fsr": True,
    #: Easy Anti-Cheat runtime (Proton ``PROTON_EAC_RUNTIME`` when available).
    "eac": True,
    #: Locale override (``LANG``/``LC_ALL``) for the game, e.g. ``ja_JP.UTF-8``.
    "locale": "",
    "env": {},
    "pre_launch": [],
    "post_launch": [],
}


@dataclass
class Game:
    """A game entry. ``source`` names the provider it came from ('local', 'steam', ...)."""

    name: str
    id: int | None = None
    sortname: str | None = None
    slug: str = ""
    runner: str = "wine"
    platform: str | None = None
    executable: str | None = None
    arguments: str | None = None
    working_dir: str | None = None
    prefix: str | None = None
    source: str = "local"
    source_id: str | None = None
    catalog_slug: str | None = None
    year: int | None = None
    installed: bool = False
    playtime: float = 0.0
    lastplayed: int | None = None
    cover: str | None = None
    banner: str | None = None
    artwork_source: str = "auto"
    lutris_slug: str | None = None
    favorite: bool = False
    hidden: bool = False
    config: dict[str, Any] = field(default_factory=dict)

    def merged_config(self, global_config: dict[str, Any]) -> dict[str, Any]:
        """Per-game values layered over the global defaults."""
        merged = {**DEFAULT_CONFIG, **global_config}
        for key, value in self.config.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            merged[key] = value
        return merged

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Game:
        data = dict(row)
        data["config"] = json.loads(data.get("config") or "{}")
        for flag in ("installed", "favorite", "hidden"):
            if flag in data:
                data[flag] = bool(data.get(flag))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("id", None)
        row["config"] = json.dumps(row["config"])
        row["installed"] = int(bool(self.installed))
        row["favorite"] = int(bool(self.favorite))
        row["hidden"] = int(bool(self.hidden))
        return row