"""Wine prefix preparation and architecture compatibility.

A Wine prefix must be created with the same ``WINEARCH`` as the wine binary that
will use it. Running a 64-bit Proton wine on a 32-bit prefix (or vice versa) is
incompatible -- Wine cannot silently convert an existing prefix's architecture.
This module detects the prefix's architecture, ensures it matches the runner,
initialises a fresh prefix with ``wineboot`` so it is ready before the game
launches, and surfaces a clear error when an existing prefix's architecture
cannot be matched without a full rebuild.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: Markers that indicate a 64-bit Windows prefix.
_64BIT_MARKERS = ("drive_c/windows/syswow64",)


def _prefix_root(prefix: str) -> Path:
    return Path(os.path.expanduser(prefix))


def detect_prefix_arch(prefix: str) -> str | None:
    """Return the prefix's architecture ("win64"|"win32") or ``None`` if it
    does not exist yet."""
    root = _prefix_root(prefix)
    if not (root / "system.reg").is_file():
        return None  # not initialised yet
    # A 64-bit prefix contains syswow64 (32-bit compatibility) alongside
    # system32; a 32-bit prefix has only system32.
    for marker in _64BIT_MARKERS:
        if (root / marker).exists():
            return "win64"
    return "win32"


def desired_arch(wine_binary: str) -> str:
    """The architecture a wine binary expects (win32/win64).

    Best-effort: Proton and 64-bit builds want win64. Anything else defaults to
    win64 as well -- almost every modern game is 64-bit and Wine's default is
    win64 when WINEARCH is unset.
    """
    return "win64"


def prepare_prefix(
    wine_binary: str,
    prefix: str,
    *,
    steam_run: bool = False,
) -> None:
    """Initialise ``prefix`` for ``wine_binary`` if it is empty or has a
    mismatched architecture.

    Creates the prefix directory, runs ``wineboot`` (inside ``steam-run`` when
    set) so the prefix is ready before a game launches. Raises ``ValueError``
    if an existing prefix's architecture does not match the runner; in that
    case the user should delete the prefix so it is recreated correctly.
    """
    root = _prefix_root(prefix)
    root.mkdir(parents=True, exist_ok=True)

    existing = detect_prefix_arch(prefix)
    required = desired_arch(wine_binary)
    if existing is not None and existing != required:
        raise ValueError(
            f"Prefix {prefix} is a {existing} prefix but the selected runner needs "
            f"{required}. Delete the prefix so Vitrine can recreate it for this runner."
        )

    # If the prefix already exists and matches, nothing to do.
    if existing == required and not _needs_boot(root):
        return

    _run_wineboot(wine_binary, prefix, steam_run=steam_run)


def _needs_boot(root: Path) -> bool:
    """True when the prefix is fresh (no wineboot output yet)."""
    return not (root / "drive_c" / "windows" / "system32").is_dir()


def _run_wineboot(wine_binary: str, prefix: str, *, steam_run: bool) -> None:
    """Run ``wineboot`` on the (fresh) prefix to initialise it."""
    env = dict(os.environ)
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    env["WINEARCH"] = "win64"
    env["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d"
    wineserver = _siblings_binary(wine_binary, "wineserver")
    command = [wine_binary, "wineboot", "-i"]
    if steam_run and shutil.which("steam-run"):
        command = ["steam-run", *command]
    logger.info("Preparing prefix %s with %s", prefix, wine_binary)
    try:
        subprocess.run(command, env=env, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("prefix init for %s failed: %s", prefix, exc)
    if wineserver:
        try:
            subprocess.run(["steam-run", wineserver, "-w"] if steam_run else [wineserver, "-w"], env=env, timeout=60)
        except Exception:  # noqa: BLE001 - waitserver cleanup is best-effort
            pass


def _siblings_binary(wine_binary: str, name: str) -> str | None:
    """Return a sibling binary (e.g. wineserver) next to the wine executable."""
    bin_dir = Path(os.path.expanduser(wine_binary)).parent
    candidate = bin_dir / name
    return str(candidate) if candidate.is_file() else None


def open_winecfg_command(
    wine_binary: str,
    prefix: str,
    *,
    steam_run: bool = False,
) -> list[str]:
    """Command to open the runner's Wine configuration (winecfg) for ``prefix``."""
    winecfg = _siblings_binary(wine_binary, "winecfg")
    if not winecfg:
        winecfg = wine_binary  # fall back; `wine winecfg` also opens it
    command = [winecfg]
    if steam_run and shutil.which("steam-run"):
        command = ["steam-run", *command]
    env = dict(os.environ)
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    env["WINEARCH"] = "win64"
    return command, env