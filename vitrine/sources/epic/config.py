"""Locating Epic-related directories.

Two families of data are used:

- The **Epic Games Store** (EGS) under Wine records what is installed in
  ``Manifests`` as ``<appid>.item`` JSON files. Reading them lets Vitrine mark
  which Epic titles are actually installed locally.
- **legendary** keeps its own database and config under
  ``~/.config/legendary`` and installs games under ``~/Games`` by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Environment override (used by tests and unusual setups).
EGS_PREFIX_ENV = "VITRINE_EGS_PREFIX"
#: Env override for the manifests folder (overrides discover_from_prefix).
EGS_MANIFESTS_ENV = "VITRINE_EGS_MANIFESTS"

#: Relative path of the manifests dir inside an EGS Wine prefix (drive_c).
_MANIFEST_REL = Path("drive_c") / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"

#: Common Wine-prefix locations for an EGS install.
COMMON_PREFIXES = (
    "~/.local/share/vitrine/prefixes/epic",
    "~/Games/epic-games",
    "~/Games/epic-games-store",
    "~/.wine",
)


def egs_manifests_dir() -> Path | None:
    """Return the EGS Manifests dir, or ``None`` if none is found."""
    if override := os.environ.get(EGS_MANIFESTS_ENV):
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    if prefix := os.environ.get(EGS_PREFIX_ENV):
        path = Path(prefix).expanduser() / _MANIFEST_REL
        if path.is_dir():
            return path
    for candidate in COMMON_PREFIXES:
        path = Path(candidate).expanduser() / _MANIFEST_REL
        if path.is_dir():
            return path
    return None


def legendary_config_dir() -> Path:
    """Return legendary's config dir (first existing, else the default)."""
    for candidate in ("~/.config/legendary", "~/.config/legendary-gl"):
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return Path("~/.config/legendary").expanduser()


def legendary_credentials_file() -> Path:
    """legendary keeps its login session in ``credentials`` inside its config."""
    return legendary_config_dir() / "credentials"


def load_manifest(path: Path) -> dict | None:
    """Parse a single EGS ``.item`` manifest file."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installed_manifests() -> list[dict]:
    """Return every installed game's manifest across the EGS manifests dir."""
    manifests_dir = egs_manifests_dir()
    if manifests_dir is None:
        return []
    out: list[dict] = []
    try:
        entries = sorted(manifests_dir.glob("*.item"))
    except OSError:
        return []
    for path in entries:
        data = load_manifest(path)
        if data is None:
            continue
        if data.get("bIsInstalled") is False:
            continue
        out.append(data)
    return out