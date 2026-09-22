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


def test_native_linux_game_is_launched_directly() -> None:
    command = launch.build_command(game(runner="linux"), {})

    assert command == ["/games/Game.exe"]


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


def test_detect_gog_executable_skips_installers(tmp_path) -> None:
    from vitrine.launch import detect_gog_executable

    root = tmp_path / "p"
    (root / "drive_c" / "Program Files").mkdir(parents=True)
    (root / "drive_c" / "Program Files" / "setup.exe").write_text("")
    (root / "drive_c" / "Program Files" / "GameLauncher.exe").write_text("")
    exe = detect_gog_executable(root)
    assert exe == "C:\\\\Program Files\\\\GameLauncher.exe"
    assert "setup" not in exe.lower()


def test_detect_gog_executable_prefers_gog_games_dir(tmp_path) -> None:
    from vitrine.launch import detect_gog_executable

    root = tmp_path / "p"
    (root / "drive_c" / "GOG Games" / "HuniePop").mkdir(parents=True)
    (root / "drive_c" / "GOG Games" / "HuniePop" / "HuniePop.exe").write_text("")
    (root / "drive_c" / "GOG Games" / "HuniePop" / "unins000.exe").write_text("")
    assert detect_gog_executable(root) == "C:\\\\GOG Games\\\\HuniePop\\\\HuniePop.exe"


def test_detect_gog_executable_none_when_empty(tmp_path) -> None:
    from vitrine.launch import detect_gog_executable

    assert detect_gog_executable(tmp_path / "missing") is None


def _fake_proton(tmp_path):
    """A fake Proton dist: <root>/files/bin/wine + toolmanifest.vdf."""
    root = tmp_path / "Proton 11"
    files = root / "files"
    (files / "bin").mkdir(parents=True)
    (files / "bin" / "wine").write_text("#!/bin/sh\n")
    (files / "bin" / "wine").chmod(0o755)
    (root / "toolmanifest.vdf").write_text("{}")
    return root, files / "bin" / "wine"


def test_is_proton_path_detects_proton(tmp_path) -> None:
    from vitrine.launch import _is_proton_path

    root, wine = _fake_proton(tmp_path)
    assert _is_proton_path(str(wine)) is True
    plain = tmp_path / "wine64"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o755)
    assert _is_proton_path(str(plain)) is False


def test_proton_dist_dir_resolves(tmp_path) -> None:
    from vitrine.launch import _proton_dist_dir

    root, wine = _fake_proton(tmp_path)
    assert _proton_dist_dir(str(wine)) == str(root)


def test_wine_command_routes_proton_through_umu(
    tmp_path, monkeypatch
) -> None:
    from vitrine import launch

    root, wine = _fake_proton(tmp_path)
    monkeypatch.setenv("VITRINE_UMU", "/opt/umu-run")
    game = Game(name="G", runner="wine", executable="/games/G.exe")
    cmd = launch.wine_command(game, {"wine_binary": str(wine)})
    assert cmd == ["/opt/umu-run", "/games/G.exe"]


def test_build_env_sets_umu_vars_for_proton(tmp_path, monkeypatch) -> None:
    from vitrine import launch

    root, wine = _fake_proton(tmp_path)
    game = Game(name="G", runner="wine", slug="g-slug", executable="/games/G.exe")
    env = launch.build_env(game, {"wine_binary": str(wine)})
    assert env["PROTONPATH"] == str(root)
    assert env["GAMEID"] == "g-slug"
    assert env["STEAM_COMPAT_DATA_PATH"]
