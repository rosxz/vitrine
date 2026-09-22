"""Unified launcher (umu-run) for Proton/GE-Proton games.

umu-run is the launcher Lutris and Heroic use for all Wine/Proton integration.
Given a game executable, a Proton distribution and a prefix, it handles the
prefix setup (runtime DLLs, drive mappings, wineserver), path mapping and
proton-fixes that Vitrine previously hand-rolled (seeding default_pfx, creating
dos drives, installing vkd3d/dxvk). It needs a Steam Runtime (steamrt4) fetched
on first use.

This module builds the umu command and the env vars it expects: ``PROTONPATH``
(the Proton dist), ``GAMEID``, ``WINEPREFIX`` and the Steam compat env. Engine-
side, GUI-free.
"""

from __future__ import annotations

import os
import shutil

#: Env override for the bundled umu-run (set by the flake, mirrors VITRINE_*).
UMU_ENV = "VITRINE_UMU"


class UmuError(Exception):
    """Raised when umu-run is unavailable."""


def umu_binary() -> str:
    """Return the umu-run executable path, or raise if unavailable."""
    override = os.environ.get(UMU_ENV)
    if override:
        return override
    path = shutil.which("umu-run")
    if not path:
        raise UmuError(
            "umu-run is not available. Install the 'umu-launcher' package, or set "
            f"{UMU_ENV} to its path to launch Proton games."
        )
    return path


def is_available() -> bool:
    try:
        umu_binary()
        return True
    except UmuError:
        return False


def umu_env(
    prefix: str,
    proton_path: str | None = None,
    game_id: str = "umu-default",
) -> dict[str, str]:
    """Return the environment variables umu-run needs to launch a game.

    ``proton_path`` is the Proton distribution directory (e.g.
    ``.../Proton 11.0``). When omitted, umu uses its default/latest Proton.
    """
    env = dict(os.environ)
    if "GAMEID" not in env:
        env["GAMEID"] = game_id
    if "WINEPREFIX" not in env:
        env["WINEPREFIX"] = os.path.expanduser(prefix)
    if "PROTONPATH" not in env and proton_path:
        env["PROTONPATH"] = proton_path
    # Steam compat env (some Proton versions/tools expect these).
    env["STEAM_COMPAT_DATA_PATH"] = env.get("WINEPREFIX")
    env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", os.path.expanduser("~/.local/share/Steam"))
    return env


def umu_command(executable: str, args: list[str] | None = None) -> list[str]:
    """Build the ``umu-run <executable> [args...]`` command line.

    The first non-option argument is the program to run under Proton/Wine; umu
    passes any remaining arguments to it. ``executable`` should be an absolute
    host path (self-mapped via Wine).
    """
    command = [umu_binary(), executable]
    if args:
        command += list(args)
    return command