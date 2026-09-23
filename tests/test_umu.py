"""Tests for the umu-run (unified launcher) wrapper."""

from __future__ import annotations

import pytest

from vitrine.wine import umu


def test_umu_binary_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")
    assert umu.umu_binary() == "/opt/umu-run"
    assert umu.is_available()


def test_umu_binary_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(umu.UMU_ENV, raising=False)
    monkeypatch.setattr(umu.shutil, "which", lambda _n: None)
    assert not umu.is_available()
    with pytest.raises(umu.UmuError):
        umu.umu_binary()


def test_umu_env_is_clean_and_sets_protocol_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pollute like the app might; umu_env must NOT leak LD_LIBRARY_PATH.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/gtk/lib")
    env = umu.umu_env(
        "/home/u/.local/share/vitrine/prefixes/game",
        proton_path="/proton",
        game_id="abc",
        install_path="/games/slug",
    )
    assert env["GAMEID"] == "abc"
    assert env["WINEPREFIX"] == "/home/u/.local/share/vitrine/prefixes/game"
    assert env["PROTONPATH"] == "/proton"
    assert env["WINEARCH"] == "win64"
    assert "LD_LIBRARY_PATH" not in env
    assert "STEAM_RUNTIME_LIBRARY_PATH" not in env
    assert env["STEAM_COMPAT_DATA_PATH"] == env["WINEPREFIX"]
    assert env["STEAM_COMPAT_INSTALL_PATH"] == "/games/slug"
    assert env["STEAM_COMPAT_MOUNTS"] == "/games/slug"
    assert "/usr/bin:/bin" in env["PATH"]


def test_umu_env_never_leaks_vk_icd_filenames(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pointing the Vulkan loader at the Nix mesa ICD that pressure-vessel
    doesn't stage makes DXVK fail to init and the game exit without a window.
    VK_ICD_FILENAMES must never reach the umu/Proton env, even if the parent
    process exports it."""

    monkeypatch.setenv("VK_ICD_FILENAMES", "/nix/store/mesa/share/vulkan/icd.d/intel_icd.x86_64.json")
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", install_path="/g")
    assert "VK_ICD_FILENAMES" not in env


def test_umu_env_always_sets_game_and_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the caller passes, GAMEID and PROTON_VERB are fixed.
    monkeypatch.delenv("GAMEID", raising=False)
    env = umu.umu_env("/p", proton_path="/proton", game_id="other")
    assert env["GAMEID"] == "other"
    assert env["PROTON_VERB"] == "waitforexitandrun"


def test_umu_env_extra_does_not_override_core(monkeypatch: pytest.MonkeyPatch) -> None:
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", extra={"GAMEID": "evil", "FOO": "1"})
    assert env["GAMEID"] == "abc"  # core wins
    assert env["FOO"] == "1"


def test_umu_command_wraps_in_steam_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")

    def _which(name):
        return "/run/current-system/sw/bin/steam-run" if name == "steam-run" else None

    monkeypatch.setattr(umu.shutil, "which", _which)
    cmd = umu.umu_command("/games/Game.exe", ["--fullscreen"])
    assert cmd == ["steam-run", "/opt/umu-run", "/games/Game.exe", "--fullscreen"]


def test_umu_command_without_fhs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")
    cmd = umu.umu_command("/games/Game.exe", fhs=False)
    assert cmd == ["/opt/umu-run", "/games/Game.exe"]

def test_umu_env_discovers_xauthority_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """GUI-launched Vitrine may not export XAUTHORITY; discover it so the X11
    game window can authenticate to Xwayland."""

    auth = tmp_path / "xauth"
    auth.write_text("auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(umu, "_discover_xauthority", lambda: str(auth))
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc")
    assert env["XAUTHORITY"] == str(auth)


def test_umu_env_keeps_existing_xauthority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/custom/auth")

    def _discover():
        return "/should/not/override"

    monkeypatch.setattr(umu, "_discover_xauthority", _discover)
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc")
    assert env["XAUTHORITY"] == "/custom/auth"


def test_umu_env_no_display_does_not_add_xauthority(monkeypatch: pytest.MonkeyPatch) -> None:
    # No DISPLAY and no XAUTHORITY in env: nothing should be synthesized.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.setattr(umu, "_discover_xauthority", lambda: "/x")
    env = umu.umu_env("/p", proton_path="/proton", game_id="abc", install_path="/g")
    assert "XAUTHORITY" not in env


def test_discover_xauthority_finds_mutter_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    auth = tmp_path / ".mutter-Xwaylandauth.ABC"
    auth.write_text("auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert umu._discover_xauthority() == str(auth)
