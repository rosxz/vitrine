"""Unified launcher (umu-run) for Proton/GE-Proton games.

umu-run is the launcher Lutris and Heroic use for all Wine/Proton integration.
Given a game executable, a Proton distribution and a prefix, it handles the
prefix setup (runtime DLLs, drive mappings, wineserver), path mapping and
proton-fixes that Vitrine previously hand-rolled (seeding default_pfx, creating
dos drives, installing vkd3d/dxvk). It needs a Steam Runtime (steamrt4) fetched
on first use.

On NixOS, umu's inner Steam Runtime (pressure-vessel) must run inside an FHS
environment (``steam-run``) so it can build its sandbox (a working ``/usr`` and
``ld.so.cache``). Running it with a polluted ``LD_LIBRARY_PATH`` (e.g. Nix store
GTK/GL libs) breaks pressure-vessel with "pv-adverb: Cannot create temporary
directory". So we launch it through ``steam-run`` with a clean, minimal
environment, only carrying the display/auth vars the game needs.
"""

from __future__ import annotations

import os
import shutil

#: Env override for the bundled umu-run (set by the flake, mirrors VITRINE_*).
UMU_ENV = "VITRINE_UMU"

#: Env vars passed through to the umu/Proton process (everything else is dropped
#: so pressure-vessel gets a clean FHS environment inside steam-run).
_UMP_PASSTHROUGH = (
    "HOME",
    "USER",
    "LOGNAME",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XAUTHORITY",
    "LANG",
    "LC_ALL",
    "HOST_LC_ALL",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "VK_ICD_FILENAMES",
    "LIBGL_DRIVERS_PATH",
    "MESA_DRIVER_PATH",
    "UMU_LOG",
    "PROTON_LOG",
)


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
    extra: dict[str, str] | None = None,
    install_path: str | None = None,
) -> dict[str, str]:
    """Return a clean environment for launching a game through umu.

    Builds a minimal env (only the passthrough vars) so pressure-vessel inside
    steam-run isn't polluted by the app's Nix ``LD_LIBRARY_PATH``. Always sets
    ``GAMEID``, ``WINEPREFIX`` and ``PROTONPATH``. ``install_path`` is the game's
    directory and is also added to ``STEAM_COMPAT_INSTALL_PATH`` +
    ``STEAM_COMPAT_MOUNTS`` so Proton maps the game drive correctly.
    """
    env: dict[str, str] = {}
    for key in _UMP_PASSTHROUGH:
        if key in os.environ and os.environ[key] != "":
            env[key] = os.environ[key]
    # A safe, minimal PATH for the sandboxed FHS.
    env.setdefault("PATH", "/usr/bin:/bin:/run/current-system/sw/bin")
    env["GAMEID"] = game_id
    env["WINEARCH"] = "win64"
    env["PROTON_VERB"] = "waitforexitandrun"
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    if proton_path:
        env["PROTONPATH"] = proton_path
    env["STEAM_COMPAT_DATA_PATH"] = env["WINEPREFIX"]
    env["STEAM_COMPAT_INSTALL_PATH"] = os.path.expanduser(install_path or "~/Games")
    env["STEAM_COMPAT_MOUNTS"] = env["STEAM_COMPAT_INSTALL_PATH"]
    if extra:
        # Never let extra override the core umu vars.
        for key, value in extra.items():
            if key not in env:
                env[key] = value
    return env


def umu_command(executable: str, args: list[str] | None = None, *, fhs: bool = True) -> list[str]:
    """Build the ``steam-run? umu-run <executable> [args...]`` command line.

    On NixOS the whole umu invocation is wrapped in ``steam-run`` (NixOS's FHS
    bwrap) so Steam Runtime 4 / pressure-vessel can build its sandbox. The first
    non-option argument is the program to run under Proton/Wine.
    """
    command: list[str] = []
    if fhs and shutil.which("steam-run"):
        command.append("steam-run")
    command.append(umu_binary())
    command.append(executable)
    if args:
        command += list(args)
    return command