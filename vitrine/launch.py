"""Turning a stored game into a runnable command line.

The whole launch pipeline lives here: Wine/Proton selection, prefix and DLL
overrides, the gamescope wrapper, and the optional MangoHud/GameMode prefixes.
Wrapping order, outermost first: gamemoderun -> mangohud -> gamescope -> wine.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .gpu import driver_env
from .library import Game
from .runners import resolve_runner
from .util import expand


@dataclass
class LaunchPlan:
    """A resolved command line plus the environment and directory it needs."""

    command: list[str]
    env: dict[str, str]
    working_dir: str | None = None
    prefix: str | None = None

    def pretty(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)


def wine_prefix_for(game: Game) -> Path:
    """The prefix a game uses: its own if set, otherwise a per-game default."""
    if game.prefix:
        resolved = expand(game.prefix)
        if resolved:
            return Path(resolved)
    return paths.prefixes_dir() / (game.slug or "game")


def detect_gog_executable(prefix: str | Path) -> str | None:
    """Locate the most likely game executable inside a GOG-installed prefix.

    GOG offline installers extract into the game's wine prefix but Vitrine never
    learns where the main executable landed. Heuristic, in order of preference:
    a ``.exe`` directly under ``<prefix>/drive_c/GOG Games``, then the newest
    ``.exe`` under the drive root, skipping installers and support tools.
    Returns an absolute ``C:``-style Windows path usable by Wine.
    """
    prefix_path = Path(prefix)
    root = prefix_path / "drive_c"
    if not root.is_dir():
        return None

    def _win_path(p: Path) -> str:
        rel = p.relative_to(root).as_posix()
        return "C:\\\\" + rel.replace("/", "\\\\")

    best: list[str] = []
    target = root / "GOG Games"
    candidates = []
    if target.is_dir():
        candidates += sorted(target.rglob("*.exe"))
    else:
        candidates += sorted(root.rglob("*.exe"))
    for exe in candidates:
        name = exe.stem.lower()
        if any(skip in name for skip in ("setup", "install", "unins", "redist", "_commonredist")):
            continue
        best.append(_win_path(exe))
    return best[0] if best else None


def apply_performance_env(env: dict[str, str], config: dict) -> None:
    """Apply per-game performance/anti-cheat switches (Lutris semantics).

    - ``esync``/``fsync``: set ``WINEESYNC``/``WINEFSYNC`` (and Proton's inverted
      ``PROTON_NO_ESYNC``/``PROTON_NO_FSYNC`` when disabled).
    - ``fsr``: AMD FidelityFX Super Resolution via ``WINE_FULLSCREEN_FSR``.
    - ``eac``: point ``PROTON_EAC_RUNTIME`` at Vitrine's EAC runtime dir when the
      user has placed one there (mirrors Lutris's ``eac_runtime``).
    """
    env["WINEESYNC"] = "1" if config.get("esync", True) else "0"
    env["WINEFSYNC"] = "1" if config.get("fsync", True) else "0"
    if not config.get("esync", True):
        env["PROTON_NO_ESYNC"] = "1"
    if not config.get("fsync", True):
        env["PROTON_NO_FSYNC"] = "1"
    if config.get("fsr", True):
        env["WINE_FULLSCREEN_FSR"] = "1"
    if config.get("eac", True):
        runtime = paths.data_dir() / "eac_runtime"
        if runtime.is_dir():
            env["PROTON_EAC_RUNTIME"] = str(runtime)


def build_env(game: Game, config: dict) -> dict[str, str]:
    """Environment for the game: prefix, DLL overrides, sync, user variables."""
    env = dict(os.environ)
    env["WINEPREFIX"] = str(wine_prefix_for(game))

    # Proton builds use the umu launcher, which expects PROTONPATH/GAMEID and the
    # Steam compat env. Set those so the generic wine path (local/GOG) also goes
    # through umu's proper prefix/runtime setup.
    wine_binary = str(config.get("wine_binary") or "wine")
    isolate = False
    if _is_proton_path(wine_binary):
        from .wine import umu

        env = umu.umu_env(
            env["WINEPREFIX"],
            proton_path=_proton_dist_dir(wine_binary),
            game_id=game.slug or str(game.source_id or "game"),
            install_path=os.path.dirname(expand(game.executable)) if game.executable else None,
        )
        isolate = True
    if not isolate:
        env = driver_env(env)
    else:
        from .gpu import discover

        # For umu/pressure-vessel we must NOT inject the app's Nix LD_LIBRARY_PATH
        # (it breaks the Steam Runtime sandbox). Only surface the driver env vars
        # that don't mutate the loader search path.
        found = discover()
        if found.icd_json:
            env.setdefault("VK_ICD_FILENAMES", found.icd_json)
        if found.dri_dir:
            env.setdefault("LIBGL_DRIVERS_PATH", found.dri_dir)
            env.setdefault("MESA_DRIVER_PATH", found.dri_dir)

    overrides: list[str] = []
    if not config.get("dxvk", True):
        overrides += ["d3d10core=n", "d3d11=n", "dxgi=n"]
    if not config.get("vkd3d", True):
        overrides.append("d3d12=n")
    # DirectX 9/10/11 runtime DLLs (d3dx9_43, d3dcompiler_43, ...) that Wine
    # doesn't bundle; without them old games fail with "d3dx9_43.dll not found".
    if config.get("d3d_extras", True):
        d3d = install_d3d_extras(env["WINEPREFIX"])
        if d3d:
            overrides.append(d3d)
    if config.get("dll_overrides"):
        overrides.append(str(config["dll_overrides"]))
    if overrides:
        env["WINEDLLOVERRIDES"] = ";".join(overrides)

    apply_performance_env(env, config)

    # MangoHud is skipped under gamescope: gamescope draws its own overlay, and
    # MANGOHUD=1 inside a gamescope session makes many games crash. Same rule as
    # Lutris applies.
    if config.get("mangohud") and not config.get("gamescope"):
        env["MANGOHUD"] = "1"
        env["MANGOHUD_DLSYM"] = "1"

    if config.get("gamescope") and config.get("gamescope_hdr"):
        env["DXVK_HDR"] = "1"

    if str(config.get("graphics", "x11")).lower() == "wayland":
        # Proton's supported switch. Wine builds select their Wayland driver
        # themselves when WAYLAND_DISPLAY is set and the driver is present.
        env["PROTON_ENABLE_WAYLAND"] = "1"

    # Per-game locale override (Lutris-style: sets LANG + LC_ALL).
    if config.get("locale"):
        env["LANG"] = str(config["locale"])
        env["LC_ALL"] = str(config["locale"])

    for key, value in (config.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def install_d3d_extras(prefix: str) -> str | None:
    """Install the bundled D3D runtime DLLs into ``prefix``.

    Returns a ``WINEDLLOVERRIDES`` fragment (``;``-separated ``name=n`` entries)
    for the DLLs that were installed, or ``None`` when d3d_extras is not
    available or nothing was installed. Errors are non-fatal (best effort).
    """
    from .wine import d3d_extras

    try:
        installed = d3d_extras.install_to_prefix(prefix)
    except Exception:  # noqa: BLE001 - never block a launch on D3D extras
        return None
    if not installed:
        return None
    return d3d_extras.dll_overrides(installed)


def wine_command(game: Game, config: dict) -> list[str]:
    """The innermost command: a runner, the executable and its arguments.

    Native Linux games are launched directly; everything else goes through the
    configured Wine/Proton runner (``config["wine_binary"]`` or ``wine`` on
    PATH). Proton builds launched through the unified launcher (umu-run) get the
    executable passed straight to umu, wrapped in ``steam-run`` so Steam Runtime
    4 / pressure-vessel can build its sandbox, which handles prefix + runtime.
    """
    executable = expand(game.executable)
    args: list[str] = []
    if game.arguments:
        args += shlex.split(game.arguments)
    if _is_native(game.runner):
        command: list[str] = []
        if executable:
            command.append(executable)
        return command + args

    wine_binary = str(config.get("wine_binary") or "wine")
    if _is_proton_path(wine_binary):
        from .wine import umu

        command = umu.umu_command(executable, args) if executable else []
    else:
        command = [wine_binary]
        if executable:
            command.append(executable)
        command += args
    return command


def _is_proton_path(wine_binary: str) -> bool:
    """True when ``wine_binary`` lives inside a Proton distribution.

    Proton dists sit under ``.../<Name>/files/bin/wine`` (older: ``.../<Name>/bin/
    wine``) and contain a ``proton`` script and ``toolmanifest.vdf`` at their
    root. Safe heuristic that also covers GE-Proton.
    """
    return _proton_dist_dir(wine_binary) is not None


def _proton_dist_dir(wine_binary: str) -> str | None:
    """The Proton distribution directory (umu PROTONPATH), or ``None`` if the
    wine binary is not inside a Proton build.

    Resolves ``<dist>/files/bin/wine`` (or ``<dist>/bin/wine``) to ``<dist>`` and
    confirms it by the presence of ``proton``/``toolmanifest.vdf``.
    """
    from pathlib import Path as _P

    wine_path = _P(os.path.expanduser(wine_binary))
    bin_dir = wine_path.parent
    if bin_dir.name == "bin" and bin_dir.parent.name == "files":
        dist = bin_dir.parent.parent
    elif bin_dir.name == "bin":
        dist = bin_dir.parent
    else:
        return None
    if dist.is_dir() and ((dist / "toolmanifest.vdf").is_file() or (dist / "proton").is_file()):
        return str(dist)
    return None


def _is_native(runner: str | None) -> bool:
    return runner in (None, "", "linux", "native")


def gamescope_wrap(config: dict, inner: list[str]) -> list[str]:
    """Run ``inner`` inside a gamescope session.

    Flags mirror Lutris: window mode, output resolution, frame limiter, free-form
    flags, FSR sharpness, cursor grab.
    """
    args: list[str] = []
    if config.get("gamescope_window_mode"):
        args.append(str(config["gamescope_window_mode"]))
    if config.get("gamescope_output_res"):
        width, _, height = str(config["gamescope_output_res"]).lower().partition("x")
        if width and height:
            args += ["-W", width, "-H", height]
    if config.get("gamescope_fps_limiter"):
        args += ["-r", str(config["gamescope_fps_limiter"])]
    if config.get("gamescope_flags"):
        args += shlex.split(str(config["gamescope_flags"]))
    if config.get("gamescope_fsr_sharpness"):
        args += ["--fsr-sharpness", str(config["gamescope_fsr_sharpness"])]
    if config.get("gamescope_force_grab_cursor"):
        args.append("--force-grab-cursor")
    return ["gamescope", *args, "--", *inner]


def build_command(game: Game, config: dict) -> list[str]:
    """The full wrapped command line for a game.

    Wrapping order, outermost first: gamescope, gamemoderun, mangohud, runner.
    """
    command = wine_command(game, config)
    if config.get("mangohud") and not config.get("gamescope"):
        command = ["mangohud", *command]
    if config.get("gamemode"):
        command = ["gamemoderun", *command]
    if config.get("gamescope"):
        command = gamescope_wrap(config, command)
    return command


def build_launch_plan(game: Game, config: dict, runners_store: dict[str, str] | None = None) -> LaunchPlan:
    """Resolve everything needed to start a game."""
    executable = expand(game.executable)
    working_dir = expand(game.working_dir)
    if not working_dir and executable:
        working_dir = str(Path(executable).parent)

    # Resolve the selected runner (per-game or default) to a concrete wine
    # binary so ``wine_command`` uses the right runner. Native games don't use
    # wine, so leave their config untouched.
    if not _is_native(game.runner):
        resolved = resolve_runner(config.get("runner"), runners_store, config.get("wine_binary"))
        effective = {**config, "wine_binary": resolved}
    else:
        effective = config

    return LaunchPlan(
        command=build_command(game, effective),
        env=build_env(game, effective),
        working_dir=working_dir,
        prefix=str(wine_prefix_for(game)),
    )


def launch(plan: LaunchPlan, *, capture: bool = False) -> subprocess.Popen:
    """Start the game. Callers are responsible for watching the process.

    With ``capture`` the child's stdout (plus stderr) is piped so a caller can
    stream it (e.g. into the debug log window) instead of inheriting stdio."""
    if plan.prefix:
        Path(plan.prefix).mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"env": plan.env, "cwd": plan.working_dir}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    return subprocess.Popen(plan.command, **kwargs)
