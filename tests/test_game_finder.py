"""Tests for automatic game-executable detection (Lutris-inspired)."""

from __future__ import annotations

from pathlib import Path

from vitrine.services import game_finder


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")


def test_finds_named_executable(tmp_path: Path) -> None:
    _touch(tmp_path / "CoolGame" / "CoolGame.exe")
    _touch(tmp_path / "CoolGame" / "data.pak")
    assert game_finder.find_windows_game_executable(tmp_path) == str(tmp_path / "CoolGame" / "CoolGame.exe")


def test_skips_installers_updaters_redist(tmp_path: Path) -> None:
    _touch(tmp_path / "setup.exe")
    _touch(tmp_path / "unins000.exe")
    _touch(tmp_path / "vcredist_x64.exe")
    _touch(tmp_path / "game" / "MyGame.exe")
    assert game_finder.find_windows_game_executable(tmp_path) == str(tmp_path / "game" / "MyGame.exe")


def test_prunes_system_directories(tmp_path: Path) -> None:
    _touch(tmp_path / "windows" / "system32.exe")
    _touch(tmp_path / "Common Files" / "helper.exe")
    _touch(tmp_path / "Game" / "Game.exe")
    assert game_finder.find_windows_game_executable(tmp_path) == str(tmp_path / "Game" / "Game.exe")


def test_prefers_name_matching_folder(tmp_path: Path) -> None:
    root = tmp_path / "Spelunky"
    _touch(root / "launcher.exe")
    _touch(root / "Spelunky.exe")
    # The executable named after the folder ranks above an unrelated one.
    assert game_finder.find_windows_game_executable(root) == str(root / "Spelunky.exe")


def test_returns_none_when_no_exe(tmp_path: Path) -> None:
    _touch(tmp_path / "readme.txt")
    assert game_finder.find_windows_game_executable(tmp_path) is None


def test_returns_none_for_missing_dir(tmp_path: Path) -> None:
    assert game_finder.find_windows_game_executable(tmp_path / "nope") is None
