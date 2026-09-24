"""Detect and watch games launched by the Steam client (GUI-free).

Steam doesn't expose a public API for "is this game running", so Vitrine watches
`/proc`. The reliable signal is the environment Steam sets on every process it
spawns for a game: ``SteamAppId`` (and ``SteamGameId``) in
``/proc/<pid>/environ``. Wine/Proton games are a deep tree of wrapper, wineserver
and pressure-vessel processes with the appid only in the environment (never in
argv), so we match on the env first, then fall back to an appid argv token
(Steam's ``reaper <appid> …`` supervisor) and to the game install directory
appearing in a process's executable/argv/cwd.

Everyone here runs on a background thread; the window marshals the callbacks
onto the GTK main loop.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Seconds between /proc polls.
DEFAULT_INTERVAL = 4.0


def _all_pids() -> list[int]:
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return []


def _proc_argv(pid: int) -> list[str]:
    """The argv of ``pid`` as decoded, whitespace-stripped tokens."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return []
    return [token.decode("utf-8", "replace") for token in raw.split(b"\x00") if token.strip()]


def _proc_environ(pid: int) -> dict[str, str]:
    """The key=value environment of ``pid`` (same-uid processes only)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return {}
    result: dict[str, str] = {}
    for pair in raw.split(b"\x00"):
        if not pair:
            continue
        key, sep, value = pair.partition(b"=")
        if sep:
            result[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return result


def _proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as handle:
            return [int(token) for token in handle.read().split() if token.strip()]
    except (OSError, ValueError):
        return []


def _pid_matches(appid: str, pid: int, installdir: str | None = None) -> bool:
    env = _proc_environ(pid)
    if env.get("SteamAppId") == appid or env.get("SteamGameId") == appid:
        return True  # Steam sets this on every process it spawns for a game.

    argv = _proc_argv(pid)
    if not installdir:
        return appid in argv  # Steam's `reaper <appid> …` supervisor.
    needle = installdir.lower()
    haystack = " ".join(argv).lower() + " " + _proc_exe(pid).lower() + " " + _proc_cwd(pid).lower()
    # With an install dir known, additionally demand the process actually
    # references it, so a stray "reaper <appid>" for another location doesn't
    # match.
    return (appid in argv or needle in haystack) and needle in haystack


#: Wine processes that carry ``SteamAppId`` but are still alive after the game
#: itself has exited (wineserver lingers between sessions).
_WINE_INTERNAL_BASENAMES = {
    "wineserver",
    "wineserver32",
    "wineboot.exe",
    "winedevice.exe",
    "services.exe",
    "explorer.exe",
    "rpcss.exe",
    "svchost.exe",
    "plugplay.exe",
    "tabtip.exe",
    "xalia.exe",
    "mmdevapi.exe",
    "csrss.exe",
    "rundll32.exe",
    "start.exe",
    "taskmgr.exe",
    "regedit.exe",
    "wineconsole.exe",
}


def _is_wine_internal(pid: int) -> bool:
    """Whether ``pid`` is a Wine-internal service rather than the game itself.

    Wine keeps a persistent ``wineserver`` plus a handful of windowing/DLL
    services (services.exe, explorer.exe, ...) running for the prefix. Their argv
    is a Windows system path or a Wine binary name; the actual game process is
    neither. Filtering these out makes the watcher flip to "closed" the moment
    the game quits, even while Wine's background services linger.
    """
    argv = _proc_argv(pid)
    joined = " ".join(argv).lower()
    if "c:\\windows\\" in joined or "c:/windows/" in joined:
        return True
    basename = os.path.basename(_proc_exe(pid)).lower()
    return basename in _WINE_INTERNAL_BASENAMES


def steam_game_pids(appid: str, installdir: str | None = None) -> list[int]:
    """PIDs belonging to the game's process tree (env `SteamAppId`, an appid argv
    token, or the process referencing the game's install directory). Includes
    Wine-internal helpers so ``terminate_game`` can tear the whole tree down."""
    return [pid for pid in _all_pids() if _pid_matches(appid, pid, installdir)]


def steam_game_is_running(appid: str, installdir: str | None = None) -> bool:
    """Whether the game is actually running.

    Same matching as :func:`steam_game_pids`, but Wine-internal background
    processes (wineserver/services) are ignored so the game is only "running"
    while its real process lives.
    """
    return any(
        not _is_wine_internal(pid)
        for pid in steam_game_pids(appid, installdir)
    )


def terminate_tree(pid: int) -> None:
    """SIGTERM ``pid``'s descendants first, then ``pid`` itself."""
    for child in _children(pid):
        terminate_tree(child)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_game(appid: str, installdir: str | None = None) -> int:
    """Best-effort SIGTERM to the detected game processes; returns how many PIDs
    were signalled."""
    sent = 0
    for pid in list(set(steam_game_pids(appid, installdir))):
        terminate_tree(pid)
        sent += 1
    return sent


def kill_tree(pid: int) -> None:
    """SIGKILL ``pid``'s descendants first, then ``pid`` itself."""
    for child in _children(pid):
        kill_tree(child)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_game(appid: str, installdir: str | None = None) -> int:
    """Force-kill (SIGKILL) the detected game processes; returns how many PIDs
    were signalled. Used as a fallback when SIGTERM is ignored (e.g. Steam
    clients that don't respond)."""
    sent = 0
    for pid in list(set(steam_game_pids(appid, installdir))):
        kill_tree(pid)
        sent += 1
    return sent


class SteamSessionWatcher:
    """Polls /proc until a Steam-launched game stops.

    Fires ``on_start`` the first time the process is seen and ``on_exit`` when it
    disappears again (which also stops the poll). Everything runs on one daemon
    thread; callbacks are plain calls (marshal to the UI thread yourself).
    """

    def __init__(
        self,
        appid: str,
        installdir: str | None = None,
        on_start: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self.appid = appid
        self.installdir = installdir
        self.on_start = on_start
        self.on_exit = on_exit
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="steam-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.interval + 1.0)

    def _run(self) -> None:
        running = False
        while not self._stop.is_set():
            now_running = steam_game_is_running(self.appid, self.installdir)
            if now_running and not running:
                running = True
                if self.on_start is not None:
                    try:
                        self.on_start()
                    except Exception:  # noqa: BLE001
                        logger.exception("steam on_start failed")
            elif not now_running and running:
                running = False
                if self.on_exit is not None:
                    try:
                        self.on_exit()
                    except Exception:  # noqa: BLE001
                        logger.exception("steam on_exit failed")
                # Session over; stop watching.
                self._stop.set()
                return
            self._stop.wait(self.interval)