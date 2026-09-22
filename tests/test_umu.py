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


def test_umu_env_sets_protocol_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    env = umu.umu_env("/home/u/.local/share/vitrine/prefixes/game", proton_path="/proton", game_id="abc")
    assert env["GAMEID"] == "abc"
    assert env["WINEPREFIX"] == "/home/u/.local/share/vitrine/prefixes/game"
    assert env["PROTONPATH"] == "/proton"
    assert env["STEAM_COMPAT_DATA_PATH"] == env["WINEPREFIX"]
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"]


def test_umu_env_does_not_override_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAMEID", "preset")
    env = umu.umu_env("/p", proton_path="/proton", game_id="other")
    # GAMEID from the environment is preserved (does not replace).
    assert env["GAMEID"] == "preset"


def test_umu_command_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(umu.UMU_ENV, "/opt/umu-run")
    cmd = umu.umu_command("/games/Game.exe", ["--fullscreen"])
    assert cmd == ["/opt/umu-run", "/games/Game.exe", "--fullscreen"]