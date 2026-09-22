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