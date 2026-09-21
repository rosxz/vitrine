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


def build_env(game: Game, config: dict) -> dict[str, str]:
    """Environment for the game: prefix, DLL overrides, sync, user variables."""
    env = dict(os.environ)
    env["WINEPREFIX"] = str(wine_prefix_for(game))

    overrides: list[str] = []
    if not config.get("dxvk", True):
        overrides += ["d3d10core=n", "d3d11=n", "dxgi=n"]
    if not config.get("vkd3d", True):
        overrides.append("d3d12=n")
    if config.get("dll_overrides"):
        overrides.append(str(config["dll_overrides"]))
    if overrides:
        env["WINEDLLOVERRIDES"] = ";".join(overrides)

    env["WINEESYNC"] = "1" if config.get("esync", True) else "0"
    env["WINEFSYNC"] = "1" if config.get("fsync", True) else "0"

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

    for key, value in (config.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def wine_command(game: Game, config: dict) -> list[str]:
    """The innermost command: a runner, the executable and its arguments.

    Native Linux games are launched directly; everything else goes through the
    configured Wine/Proton runner (``config["wine_binary"]`` or ``wine`` on
    PATH).
    """
    command: list[str] = []
    if not _is_native(game.runner):
        command.append(str(config.get("wine_binary") or "wine"))
    executable = expand(game.executable)
    if executable:
        command.append(executable)
    if game.arguments:
        command += shlex.split(game.arguments)
    return command


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


def launch(plan: LaunchPlan) -> subprocess.Popen:
    """Start the game. Callers are responsible for watching the process."""
    if plan.prefix:
        Path(plan.prefix).mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(plan.command, env=plan.env, cwd=plan.working_dir)
