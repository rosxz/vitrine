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

import json
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
    # NOTE: VK_ICD_FILENAMES is deliberately NOT passed through. Pointing the
    # Vulkan loader at the Nix mesa ICD that pressure-vessel doesn't stage inside
    # its steam-run FHS sandbox makes Proton/DXVK fail to initialise, so the game
    # exits before opening a window. Let pressure-vessel resolve its own drivers
    # (Lutris/Steam do the same).
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
    directory, kept on the API for callers but unused by umu itself (Proton
    derives the game drive itself; the resulting "unable to use parent for game
    drive" notice is benign, present on NixOS too).
    """
    env: dict[str, str] = {}
    for key in _UMP_PASSTHROUGH:
        if key in os.environ and os.environ[key] != "":
            env[key] = os.environ[key]
    # Wine presents a fullscreen X11 window for the game. Under a Wayland desktop
    # (GNOME/Mutter) an X11 window needs the Xwayland auth token, but GUI-launched
    # apps often don't export XAUTHORITY (only shells do). Discover it from the
    # runtime dir / home so the window actually maps when Vitrine is launched from
    # the desktop rather than a shell.
    if env.get("DISPLAY") and not env.get("XAUTHORITY"):
        discovered = _discover_xauthority()
        if discovered:
            env["XAUTHORITY"] = discovered
    # A safe, minimal PATH for the sandboxed FHS.
    env.setdefault("PATH", "/usr/bin:/bin:/run/current-system/sw/bin")
    env["GAMEID"] = game_id
    env["WINEARCH"] = "win64"
    env["PROTON_VERB"] = "waitforexitandrun"
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    if proton_path:
        env["PROTONPATH"] = proton_path
    _fix_flatpak_32bit_icd(env)
    if extra:
        # Never let extra override the core umu vars.
        for key, value in extra.items():
            if key not in env:
                env[key] = value
    return env


def _discover_xauthority() -> str | None:
    """Locate the Xwayland/X11 authority token for the current session.

    When XAUTHORITY isn't exported (common for desktop-launched apps on Wayland),
    look for Mutter's Xwayland auth file in ``$XDG_RUNTIME_DIR`` and the usual
    X defaults, preferring permissions of the running user.
    """
    candidates: list[str] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        import glob

        candidates += sorted(glob.glob(os.path.join(runtime, ".mutter-Xwaylandauth.*")))
    candidates += [os.path.join(os.path.expanduser("~"), ".Xauthority")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate
    return None


# Directory where the GL32 extension binds 32-bit Mesa drivers inside a Flatpak
# sandbox. This Flatpak mounts it under ``/app/lib/i386-linux-gnu/GL`` but the
# extension's own ICD JSON hardcodes ``.../GL/default/lib/...``; on the GNOME
# runtime the drivers land in ``.../GL/lib/...`` (no ``default`` segment). We
# rewrite the ICD JSONs to the real path and publish them to the loader's
# default search dir so 32-bit D3D9/Vulkan games can create a Vulkan instance.
_GL32_DIRS = (
    "/app/lib/i386-linux-gnu/GL",
    "/usr/lib/i386-linux-gnu/GL",
)


def _fix_flatpak_32bit_icd(env: dict[str, str]) -> None:
    """Make the 32-bit Vulkan ICD discoverable when running under a Flatpak.

    Only meaningful in the Flatpak build (best effort; silently skipped
    elsewhere). Writes corrected 32-bit ICD JSONs into ``$XDG_DATA_HOME/
    vulkan/icd.d`` (a path the Vulkan loader scans by default) so a 32-bit
    wine/DXVK can ``vkCreateInstance``. Nothing is done on NixOS where the Mesa
    drivers resolve normally.
    """
    import glob as _glob

    if os.environ.get("XDG_DATA_HOME", "").find(".var/app/") < 0:
        return
    try:
        icd_dir = None
        for base in _GL32_DIRS:
            cand = os.path.join(base, "lib", "vulkan", "icd.d")
            if os.path.isdir(cand):
                icd_dir = cand
                break
        if not icd_dir:
            return
        out_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "vulkan", "icd.d")
        os.makedirs(out_dir, exist_ok=True)
        for jpath in _glob.glob(os.path.join(icd_dir, "*.i686.json")):
            try:
                with open(jpath, encoding="utf-8") as fh:
                    data = json.load(fh)
                lib_path = data.get("ICD", {}).get("library_path", "")
                # The extension declares .../GL/default/lib/... but the driver
                # mounts at the concrete .../GL/lib/... (no default/). If
                # the declared driver isn't present, point at the real one.
                if not os.path.exists(lib_path):
                    real = _resolve_gl32_driver(lib_path)
                    if not real:
                        continue
                    data["ICD"]["library_path"] = real
                with open(os.path.join(out_dir, os.path.basename(jpath)), "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=4)
            except Exception:  # noqa: BLE001 - best effort, never block a launch
                continue
    except Exception:  # noqa: BLE001
        return


def _resolve_gl32_driver(declared: str) -> str | None:
    """Map a GL32 ICD ``library_path`` to where the driver actually mounts.

    The ICD JSON declares ``.../GL/default/lib/<driver>.so`` but the GNOME
    runtime binds drivers under ``.../GL/lib/<driver>.so`` (no ``default/``
    segment). Return the concrete existing path, else ``None``.
    """
    name = os.path.basename(declared)
    for base in _GL32_DIRS:
        cand = os.path.join(base, "lib", name)
        if os.path.exists(cand):
            return cand
    return None


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