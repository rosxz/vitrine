"""Automatic detection of a game's main executable inside an install folder.

Inspired by Lutris's ``lutris/util/game_finder.py`` (which uses libmagic to
identify PE executables). Vitrine avoids the ``python-magic`` dependency and
instead filters on the ``.exe`` extension plus exclusion lists, then ranks the
survivors by how well their name matches the game's folder name -- the same
tie-breaking Lutris uses. Returns host filesystem paths (Wine accepts them
directly and they are what Vitrine stores on a Game).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

#: Directories that never hold the game's own executable (Lutris's list).
_EXCLUDED_DIRS = (
    "internet explorer",
    "windows nt",
    "common files",
    "windows media player",
    "windows",
    "programdata",
    "users",
    "gamespy arcade",
)

#: Executable names that are installers/updaters/redistributables, not the game
#: (Lutris's ``is_excluded_exe`` plus the redist/installer names Vitrine already
#: skips elsewhere).
_EXCLUDED_EXE = (
    "unins000",
    "uninstal",
    "update",
    "setup",
    "config.exe",
    "gsarcade.exe",
    "dosbox.exe",
    "unitycrashhandler",
    "dxsetup",
    "vcredist",
    "redist",
    "directx",
)


def normalize_name(name: str) -> str:
    """Reduce a name to its letters/digits for folder-name matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def is_excluded_dir(path: str) -> bool:
    parts = {part.lower() for part in path.replace("\\", "/").split("/")}
    return any(excluded in parts for excluded in _EXCLUDED_DIRS)


def is_excluded_exe(filename: str) -> bool:
    lowered = filename.lower()
    if lowered.endswith(".dll"):
        return True
    return any(excluded in lowered for excluded in _EXCLUDED_EXE)


def sort_candidates(candidates: list[str], game_name: str) -> list[str]:
    """Order candidates deterministically (Lutris's ranking).

    Executables named after the game folder rank first, then those sharing a
    prefix with it, then the rest; ties break by longer name, then alphabetical.
    """

    def sort_key(path: str) -> tuple:
        basename = os.path.basename(path)
        stem = normalize_name(os.path.splitext(basename)[0])
        if game_name and stem:
            if stem == game_name:
                rank = 0
            elif stem.startswith(game_name) or game_name.startswith(stem):
                rank = 1
            else:
                rank = 2
        else:
            rank = 2
        name = os.path.splitext(basename)[0]
        return rank, -len(name), basename.lower()

    return sorted(candidates, key=sort_key)


def find_windows_game_executable(path: str | os.PathLike) -> str | None:
    """Return the most likely main ``.exe`` (host path) under ``path``, or None.

    Skips updaters/installers/redistributables and system directories, preferring
    executables named after the game folder.
    """
    root = str(path)
    if not os.path.isdir(root):
        return None
    game_name = normalize_name(os.path.basename(os.path.normpath(root)))

    for base, dirs, files in os.walk(root):
        # Prune excluded directories in place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if not is_excluded_dir(d)]
        if is_excluded_dir(base):
            continue
        candidates = defaultdict(list)
        for name in files:
            if is_excluded_exe(name):
                continue
            if not name.lower().endswith(".exe"):
                continue
            abspath = os.path.join(base, name)
            if os.path.islink(abspath):
                continue
            candidates["exe"].append(abspath)
        if candidates["exe"]:
            return sort_candidates(candidates["exe"], game_name)[0]
    return None
