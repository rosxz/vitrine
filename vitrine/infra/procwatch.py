"""Generic /proc process-tree helpers (GUI-free).

Used to distinguish a game's *own* process from the wrapper that launched it
(gamescope, mangohud, gamemoderun…). When a wrapped game exits, wrappers can
linger (gamescope keeps an (invisible) window even after its child is gone), so
we watch for a marker of the game process and tear the leftover tree down.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path


def proc_argv(pid: int) -> list[str]:
    """The decoded argv tokens of ``pid`` (empty on error)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return []
    return [token.decode("utf-8", "replace") for token in raw.split(b"\x00") if token.strip()]


def children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as handle:
            return [int(token) for token in handle.read().split() if token.strip()]
    except (OSError, ValueError):
        return []


def descendants(pid: int) -> list[int]:
    """All PIDs below ``pid`` in the process tree (excludes ``pid``)."""
    result: list[int] = []
    stack = children(pid)
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(children(current))
    return result


#: Wrappers that re-run the game command (root of a wrapped session) -- they are
#: never the game process itself and may linger long after the game exits.
_WRAPPER_BASENAMES = {
    "gamescope",
    "gamescopereader",
    "gamescopereaper",
    "mangohud",
    "gamemoderun",
    "steam-run",
    "steam",
    "bwrap",
    "srt-bwrap",
    "gameoverlayui",
    ".umu-run-wrapped",
    "umu-shim",
}

#: Wine-internal background processes and Steam/overlay helpers that are never
#: "the game" (wineserver, windowing services and preloaders survive the game
#: quitting and can otherwise mask a closed game).
_WINE_INTERNAL_BASENAMES = {
    "wineserver",
    "wineserver32",
    "wineboot.exe",
    "wineboot",
    "services.exe",
    "winedevice.exe",
    "plugplay.exe",
    "explorer.exe",
    "rpcss.exe",
    "svchost.exe",
    "tabtip.exe",
    "xalia.exe",
    "mmdevapi.exe",
    "csrss.exe",
    "rundll32.exe",
    "start.exe",
    "taskmgr.exe",
    "regedit.exe",
    "wineconsole.exe",
    "winesystemstats.exe",
    "winedbg",
    "steam.exe",
    "proton",
    "python",
    "python3",
    ".umu-run-wrapped",
    "umu-shim",
    "winevdm",
    "wine-preloader",
    "wine64-preloader",
    "wine64",
    "wine32",
    "wineboot32",
    "winedevice",
    "rundll32",
}

_AUX_BASENAMES = _WRAPPER_BASENAMES | _WINE_INTERNAL_BASENAMES | {
    "pressure-vessel-wrap",
    "pv-adverb",
    "steamwebhelper",
}


def is_wrapper(pid: int) -> bool:
    """Whether ``pid`` is a wrapper (gamescope/mangohud/…) rather than the game.

    Wrappers re-run the game command and can outlive it (gamescope keeps an
    invisible window open). A lingering session is only possible when the spawned
    root is a wrapper; a plain ``wine <exe>``/native root is the game itself and
    exits with it.
    """
    argv = proc_argv(pid)
    if not argv:
        return False
    return any(
        os.path.basename(token).lower() in _WRAPPER_BASENAMES
        for token in argv
        if token
    )


def is_aux(pid: int) -> bool:
    """Whether ``pid`` is a wrapper / Wine-internal / overlay helper.

    These can be present both while the game runs and after it has quit, so they
    do not tell us whether the game is still alive. Note that ``wine`` itself is
    NOT aux -- it is the runner that hosts the game and exits with it.
    """
    argv = proc_argv(pid)
    if not argv:
        return False
    return os.path.basename(argv[0]).lower() in _AUX_BASENAMES


def game_present_in_tree(pid: int, executable: str | None = None) -> bool:
    """True while any descendant of ``pid`` is a real (non-aux) process.

    The game, once launched, is a process whose argv[0] is not a wrapper or a
    Wine-internal service (e.g. ``HuniePop.exe`` or ``python3 …/proton``). After
    it exits, only aux processes (gamescopereaper, wineserver, …) remain, which
    is how we tell a lingering wrapper from a running game.
    """
    expected = Path(executable).name.casefold() if executable else None
    for desc in descendants(pid):
        argv = proc_argv(desc)
        if is_aux(desc):
            continue
        if expected is not None:
            if any(Path(token).name.casefold() == expected for token in argv):
                # Proton's wine launcher includes the game path in its argv;
                # the actual Windows process has the game executable basename.
                if Path(argv[0]).name.casefold() == expected:
                    return True
                continue
        else:
            return True
    return False


def terminate_tree(pid: int) -> None:
    """SIGTERM ``pid``'s descendants first, then ``pid`` itself."""
    for child in children(pid):
        terminate_tree(child)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_tree(pid: int) -> None:
    """SIGKILL the whole tree rooted at ``pid`` (for stuck wrappers)."""
    for child in children(pid):
        kill_tree(child)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass