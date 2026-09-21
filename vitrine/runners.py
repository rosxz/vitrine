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

#: Common locations scanned for Wine/Proton builds (Lutris-style).
RUNNER_DIRS = (
    "~/.local/share/vitrine/runners",
    "~/.local/share/lutris/runners/wine",
    "~/Games/proton",
    "~/.steam/steam/steamapps/common/Proton",
    "~/.local/share/Steam/steamapps/common/Proton",
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
        name = Path(resolved).name or runner_id
        runners[runner_id] = Runner(runner_id, name, resolved, kind=_kind_of(resolved))

    return list(runners.values())


def _discover_on_disk() -> list[Runner]:
    found: list[Runner] = []
    seen: set[str] = set()
    for directory in RUNNER_DIRS:
        base = Path(os.path.expanduser(directory))
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            for rel in _WINE_BINS:
                candidate = child / rel
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    runner_id = _slug(child.name)
                    if runner_id in seen:
                        continue
                    seen.add(runner_id)
                    found.append(Runner(runner_id, child.name, str(candidate), kind=_kind_of(str(candidate))))
                    break
    return found


def _kind_of(path: str) -> str:
    lowered = (Path(path).name + os.pathsep + path).lower()
    return "proton" if "proton" in lowered else "wine"


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