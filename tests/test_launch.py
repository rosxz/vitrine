"""Launch command construction."""

from __future__ import annotations

import pytest

from vitrine import launch
from vitrine.library import Game


def game(**kwargs) -> Game:
    defaults = {"name": "Test Game", "slug": "test-game", "executable": "/games/Game.exe"}
    return Game(**{**defaults, **kwargs})


def test_plain_wine_command() -> None:
    command = launch.build_command(game(), {})

    assert command == ["wine", "/games/Game.exe"]


def test_arguments_are_split_with_quoting() -> None:
    command = launch.build_command(game(arguments='-windowed -name "Big Mod"'), {})

    assert command == ["wine", "/games/Game.exe", "-windowed", "-name", "Big Mod"]


def test_configured_runner_binary_wins() -> None:
    command = launch.build_command(game(), {"wine_binary": "/runners/GE-Proton9/bin/wine"})

    assert command[0] == "/runners/GE-Proton9/bin/wine"


def test_gamemode_and_mangohud_prefixes() -> None:
    command = launch.build_command(game(), {"gamemode": True, "mangohud": True})

    assert command == ["gamemoderun", "mangohud", "wine", "/games/Game.exe"]


def test_gamescope_wraps_everything_and_replaces_mangohud() -> None:
    config = {"gamemode": True, "mangohud": True, "gamescope": True, "gamescope_window_mode": "-f"}

    command = launch.build_command(game(), config)

    assert command == ["gamescope", "-f", "--", "gamemoderun", "wine", "/games/Game.exe"]


def test_gamescope_arguments_come_from_config() -> None:
    config = {
        "gamescope": True,
        "gamescope_window_mode": "-f",
        "gamescope_output_res": "2560x1440",
        "gamescope_fps_limiter": "60",
        "gamescope_flags": "--adaptive-sync",
        "gamescope_fsr_sharpness": "2",
        "gamescope_force_grab_cursor": True,
    }

    command = launch.build_command(game(), config)

    assert command == [
        "gamescope",
        "-f",
        "-W",
        "2560",
        "-H",
        "1440",
        "-r",
        "60",
        "--adaptive-sync",
        "--fsr-sharpness",
        "2",
        "--force-grab-cursor",
        "--",
        "wine",
        "/games/Game.exe",
    ]


def test_env_sets_prefix_and_overrides() -> None:
    env = launch.build_env(game(prefix="/prefixes/foo"), {"dll_overrides": "winmm=n,b"})

    assert env["WINEPREFIX"] == "/prefixes/foo"
    assert env["WINEDLLOVERRIDES"] == "winmm=n,b"
    assert env["WINEESYNC"] == "1"


def test_env_disables_dxvk_and_vkd3d_when_turned_off() -> None:
    env = launch.build_env(game(), {"dxvk": False, "vkd3d": False})

    assert "d3d11=n" in env["WINEDLLOVERRIDES"]
    assert "d3d12=n" in env["WINEDLLOVERRIDES"]


def test_default_prefix_is_derived_from_the_slug(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    game_without_prefix = game()

    assert launch.wine_prefix_for(game_without_prefix) == tmp_path / "vitrine" / "prefixes" / "test-game"


def test_user_environment_is_applied_last() -> None:
    env = launch.build_env(game(), {"env": {"DXVK_HUD": "fps", "WINEESYNC": "0"}})

    assert env["DXVK_HUD"] == "fps"
    assert env["WINEESYNC"] == "0"


def test_mangohud_environment_is_skipped_under_gamescope() -> None:
    with_hud = launch.build_env(game(), {"mangohud": True})
    under_gamescope = launch.build_env(game(), {"mangohud": True, "gamescope": True})

    assert with_hud["MANGOHUD"] == "1"
    assert with_hud["MANGOHUD_DLSYM"] == "1"
    assert "MANGOHUD" not in under_gamescope


def test_gamescope_hdr_and_wayland_switches() -> None:
    env = launch.build_env(game(), {"gamescope": True, "gamescope_hdr": True, "graphics": "wayland"})

    assert env["DXVK_HDR"] == "1"
    assert env["PROTON_ENABLE_WAYLAND"] == "1"


def test_plan_reports_working_directory_next_to_the_executable() -> None:
    plan = launch.build_launch_plan(game(), {})

    assert plan.working_dir == "/games"
    assert plan.prefix is not None
    assert plan.pretty().endswith("wine /games/Game.exe")


@pytest.mark.parametrize("value", [None, "", False])
def test_empty_config_values_do_not_wrap(value) -> None:
    command = launch.build_command(game(), {"gamescope": value, "mangohud": value, "gamemode": value})

    assert command == ["wine", "/games/Game.exe"]
