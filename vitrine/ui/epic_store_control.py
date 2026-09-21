"""Control the background Epic Games Store client process.

The user can install/run Epic games through the actual Epic Games Store (EGS)
client under Wine, which stays running in the background. These helpers let the
UI launch, focus (raise), and kill that client process. Detection matches the
EGS executable name; focusing uses the X11 ``xdotool``/``wmctrl`` tools when
available and falls back gracefully otherwise.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess

logger = logging.getLogger(__name__)

#: Executable basenames of the Epic Games Store client (Windows process under wine).
EGS_PROCESS_NAMES = ("launcher.exe", "EpicGamesLauncher.exe", "EpicWebHelper.exe")

#: Configurable EGS executable to launch (wine + exe). Empty disables "Launch".
EGS_LAUNCH_ENV = "VITRINE_EGS_LAUNCH"
#: Default wine binary used to start EGS.
EGS_WINE = "wine"


class StoreProcessError(Exception):
    """Raised when launching/focusing/killing the store fails."""


def is_running() -> bool:
    """Whether any EGS client process is currently running."""
    return bool(_egs_pids())


def _egs_pids() -> list[str]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "|".join(EGS_PROCESS_NAMES)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def launch_store() -> None:
    """Start the EGS client under wine (detached)."""
    command = (os.environ.get(EGS_LAUNCH_ENV) or "").strip()
    exe_path = command if command else None
    binary = [exe_path] if exe_path else _default_store_executable()
    if not binary:
        raise StoreProcessError(
            "No Epic Games Store executable configured. Set the EGS launch command "
            "(wine path/to/EpicGamesLauncher.exe) in settings."
        )
    try:
        subprocess.Popen(binary, env=dict(os.environ), start_new_session=True)
    except FileNotFoundError as exc:
        raise StoreProcessError(f"Could not start Epic Games Store: {exc}") from exc


def _default_store_executable() -> list[str]:
    # Without a configured exe, look for a known EGS launcher on PATH (rare).
    if shutil.which("EpicGamesLauncher.exe"):
        return ["EpicGamesLauncher.exe"]
    return []


def focus_store() -> None:
    """Raise and focus the running EGS window, if any."""
    if not _egs_pids():
        raise StoreProcessError("Epic Games Store is not running")
    # xdotool windowactivate works on X11; wmctrl -a raises the matching window.
    for tool, args in (
        ("wmctrl", ["-a", "Epic Games"]),
        ("xdotool", ["search", "--name", "Epic Games", "windowactivate", "windowraise"]),
    ):
        if shutil.which(tool):
            try:
                subprocess.run([tool, *args], capture_output=True, timeout=5)
                return
            except (OSError, subprocess.TimeoutExpired):
                continue
    # No focus tool available; at least it's running.
    logger.info("No window-focus tool found; Epic store is running but not raised")


def kill_store() -> None:
    """Terminate the running EGS client process(es)."""
    pids = _egs_pids()
    if not pids:
        raise StoreProcessError("Epic Games Store is not running")
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    logger.info("Terminated Epic Games Store: %s", ", ".join(pids))