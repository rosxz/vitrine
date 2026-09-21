"""Runner discovery: which Wine/Proton builds are installed and installable.

Vitrine keeps this GUI-free so it can run from any thread. We mirror Lutris'
runner model -- a runner is a Wine binary (or a Proton build that *contains* a
Wine binary). A few named presets are offered that don't correspond to a fixed
path:

- ``wine-64``  -- the system ``wine64``/``wine`` (Lutris "Default wine").
- ``wine-32``  -- the 32-bit system ``wine32``.
- ``wine-ge-custom`` -- a specific Wine-GE custom build namespace.

Everything else is a concrete Wine/Proton install found on disk or a location
registered in the settings.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Setting keys (kept here to avoid the GUI layer depending on paths).
RUNNERS_SETTING = "runners"  # dict: runner_id -> absolute path to the wine binary|dir
DEFAULT_PROTON_SETTING = "default_runner"

#: Named presets offered in any runner dropdown (mirrors Lutris).
PRESETS: dict[str, str] = {
    "wine-64": "Default wine (64)",
    "wine-32": "wine-32",
    "wine-ge-custom": "wine-ge-custom",
}

#: Common Wine-runner directories scanned directly (each subdir holds a Wine build).
WINE_RUNNER_DIRS = (
    "~/.local/share/vitrine/runners",
    "~/.local/share/lutris/runners/wine",
)

#: Steam data dirs whose ``steamapps/common`` may hold Proton builds.
STEAM_DATA_DIRS = (
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.local/share/steam",
    "~/snap/steam/common/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/data/Steam",
)

#: Other locations scanned for Proton/Wine builds.
EXTRA_RUNNER_DIRS = (
    "~/Games/proton",
    "~/Games/Proton",
    "~/Games/heroic/tools/proton",
    "~/Games/Heroic/tools/proton",
)

#: Typical wine executables inside a runner directory.
_WINE_BINS = ("bin/wine", "bin/wine64", "bin/wine32", "files/bin/wine", "wine", "tools/wine/wine64")


@dataclass(frozen=True)
class Runner:
    id: str
    name: str
    path: str  # absolute path to the wine binary (or "" for presets)
    kind: str = "wine"  # "wine" | "proton"

    @property
    def is_preset(self) -> bool:
        return not self.path


def list_runners(runners_store: dict[str, str] | None = None) -> list[Runner]:
    """Return every known runner: presets first, then discovered ones.

    ``runners_store`` is the persisted ``{runner_id: path}`` map (usually the
    value stored under :data:`RUNNERS_SETTING`). It is merged with anything
    auto-discovered on disk.
    """
    runners: dict[str, Runner] = {}
    for runner_id, name in PRESETS.items():
        runners[runner_id] = Runner(runner_id, name, "")

    known: dict[str, str] = dict(runners_store or {})
    # Discover builds in the standard runner directories (in case the user has
    # not curated the store yet).
    for runner in _discover_on_disk():
        known.setdefault(runner.id, runner.path)

    for runner_id, path in known.items():
        if runner_id in PRESETS:
            continue
        resolved = os.path.expanduser(path)
        runners[runner_id] = _runner_from_path(runner_id, resolved)

    return list(runners.values())


def _runner_from_path(runner_id: str, resolved: str) -> Runner:
    """Derive a runner's display name + kind from its binary path.

    The path is the actual binary (e.g. ``files/bin/wine`` or ``bin/wine``);
    the human-facing name is its parent's directory name (the Proton/Wine
    version, e.g. "Proton 11.0"), which preserves real identifiers instead of
    showing a generic "wine"/"proton" label.
    """
    path = Path(resolved)
    # Walk up from the binary to the version directory (the one holding the
    # launcher script / version subdir), i.e. skip bin/, files/, dist/.
    parent = path.parent
    name = path.name
    if parent.name in ("bin", "files", "dist"):
        grandparent = parent.parent.parent if parent.parent.name in ("bin", "files", "dist") else parent.parent
        name = grandparent.name
        if name in ("files", "dist"):
            name = grandparent.parent.name
    kind = "proton" if _looks_like_proton(path) else "wine"
    return Runner(runner_id, name or runner_id, resolved, kind=kind)


def _looks_like_proton(binary_path: Path) -> bool:
    """Heuristic: true if the binary lives under a Proton build dir.

    A Proton build contains a ``proton`` launcher script a couple of levels up
    from the wine binary (e.g. …/Proton 11.0/files/bin/wine). Falls back to
    checking the path string for "proton".
    """
    parent = binary_path.parent  # .../bin
    build_dir = parent.parent  # .../files or .../dist
    if build_dir.name in ("files", "dist"):
        build_dir = build_dir.parent  # .../Proton 11.0
    if (build_dir / "proton").is_file():
        return True
    return "proton" in str(binary_path).lower()


def _discover_on_disk() -> list[Runner]:
    """Enumerate installed Wine/Proton builds, mirroring Lutris.

    - Wine-runner dirs (``WINE_RUNNER_DIRS``): each subdirectory with a wine
      binary is a Wine build.
    - Steam ``common`` dirs: each subdirectory containing a ``proton`` script
      is a Proton build (kind ``proton``, pointed at that script).
    - Extra dirs: scanned the same way as the Steam common dirs.
    """
    found: list[Runner] = []
    seen: set[str] = set()

    for directory in WINE_RUNNER_DIRS:
        base = Path(os.path.expanduser(directory))
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            for rel in _WINE_BINS:
                candidate = child / rel
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    _add_runner(found, seen, child.name, str(candidate), "wine")
                    break

    common_dirs = _steamapps_common_dirs()
    for directory in (*common_dirs, *EXTRA_RUNNER_DIRS):
        base = Path(os.path.expanduser(directory))
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            proton_script = child / "proton"
            if proton_script.is_file():
                # A Proton build: its real wine binary lives in files/bin or
                # dist/bin, NOT the launcher script. Point the runner at that
                # binary so legendary --wine (and the dropdown) use a working
                # wine, not the python launcher.
                wine_path = _proton_wine_binary(child)
                if wine_path is not None:
                    _add_runner(found, seen, child.name, str(wine_path), "proton")
                else:
                    _add_runner(found, seen, child.name, str(proton_script), "proton")
                continue
            for rel in _WINE_BINS:
                candidate = child / rel
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    _add_runner(found, seen, child.name, str(candidate), "wine")
                    break
    return found


def _proton_wine_binary(proton_dir: Path) -> Path | None:
    """Return a Proton build's bundled wine binary (files/bin or dist/bin)."""
    for files_dir in ("files", "dist"):
        base = proton_dir / files_dir / "bin"
        for name in ("wine", "wine64", "wine32"):
            candidate = base / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _add_runner(
    found: list[Runner],
    seen: set[str],
    display: str,
    path: str,
    kind: str,
) -> None:
    runner_id = _slug(display)
    if runner_id in seen:
        return
    seen.add(runner_id)
    found.append(Runner(runner_id, display, path, kind=kind))


def _steamapps_common_dirs() -> list[Path]:
    """Every Steam ``steamapps/common`` directory (from libraryfolders too)."""
    dirs: list[Path] = []
    steam_root = _find_steam_root()
    if steam_root is not None:
        common = steam_root / "steamapps" / "common"
        if common.is_dir():
            dirs.append(common)
        # Additional library folders from libraryfolders.vdf.
        for extra_root in _library_folders(steam_root):
            common = extra_root / "steamapps" / "common"
            if common.is_dir() and common not in dirs:
                dirs.append(common)
    return dirs


def _find_steam_root() -> Path | None:
    for candidate in STEAM_DATA_DIRS:
        path = Path(os.path.expanduser(candidate))
        if path.is_dir() and (path / "steamapps").is_dir():
            return path
    return None


def _library_folders(steam_root: Path) -> list[Path]:
    """Parse ``steamapps/libraryfolders.vdf`` for extra library roots."""
    root = steam_root / "steamapps" / "libraryfolders.vdf"
    if not root.is_file():
        return []
    try:
        from .sources.steam.vdf import parse_vdf_file

        parsed = parse_vdf_file(str(root))
    except Exception:  # noqa: BLE001 - a bad VDF must not break runner discovery
        return []
    entries = parsed.get("libraryfolders") or {}
    out: list[Path] = []
    for key, value in entries.items():
        if str(key).isdigit() and isinstance(value, dict) and value.get("path"):
            out.append(Path(str(value["path"])))
    return out


def _slug(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "runner"


def resolve_runner(
    runner_id: str | None, runners_store: dict[str, str] | None = None, wine_binary: str | None = None
) -> str:
    """Return the wine binary path for ``runner_id``, else a sensible default.

    Presets resolve against what's installed: ``wine-64``/``wine-32`` consult
    the ``runners_store`` first, then the system PATH. Unknown/None returns the
    configured ``wine_binary`` or ``wine`` on PATH.
    """
    runners = {r.id: r for r in list_runners(runners_store)}
    runner = runners.get(runner_id or "")
    if runner is None:
        return wine_binary or _path_wine()
    if runner.path:
        return runner.path
    # Presets: try the store for an overridden path, else fall back to PATH.
    if runner_id in ('wine-64', 'wine-32', 'wine-ge-custom'):
        overridden = (runners_store or {}).get(runner_id or "")
        if overridden:
            return os.path.expanduser(overridden)
        if runner_id == "wine-32":
            return shutil.which("wine32") or shutil.which("wine") or "wine"
        return _path_wine()
    return wine_binary or _path_wine()


def _path_wine() -> str:
    return shutil.which("wine") or "wine"


def installed_ids(runners_store: dict[str, str] | None) -> list[str]:
    """Runner ids that exist on disk (presets included)."""
    runners = {r.id: r for r in list_runners(runners_store)}
    return [r.id for r in runners.values()]


def runner_installed(runner_id: str, runners_store: dict[str, str] | None) -> bool:
    return runner_id in installed_ids(runners_store) and (
        runner_id in PRESETS or (runners_store or {}).get(runner_id) is not None
    )


def remove_runner(runner_id: str, runners_store: dict[str, str]) -> dict[str, str]:
    """Remove ``runner_id`` from the persisted store, leaving disk intact."""
    removed = {k: v for k, v in runners_store.items() if k != runner_id}
    return removed


def install_runner(runner_id: str, path: str, runners_store: dict[str, str]) -> dict[str, str]:
    """Register a runner by its wine-binary path in the store."""
    updated = dict(runners_store)
    updated[runner_id] = path
    return updated


def runner_path(runner_id: str, runners_store: dict[str, str] | None) -> str:
    """Absolute path to the runner's wine binary, or ``""`` if unknown."""
    runner = next((r for r in list_runners(runners_store) if r.id == runner_id), None)
    return runner.path if runner else ""


def load_runners_store(library_store: Any) -> dict[str, str]:
    """Read the persisted runner map from a settings accessor object."""
    value = library_store.setting(RUNNERS_SETTING, {}) if hasattr(library_store, "setting") else {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def save_runners_store(library_store: Any, store: dict[str, str]) -> None:
    if hasattr(library_store, "set_setting"):
        library_store.set_setting(RUNNERS_SETTING, store)  # type: ignore[arg-type]
    else:
        logger.warning("Settings store cannot persist runners")