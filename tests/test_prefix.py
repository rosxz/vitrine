"""Tests for wine prefix preparation and architecture compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from vitrine.prefix import (
    detect_prefix_arch,
    open_winecfg_command,
    prepare_prefix,
)


def _fake_wine(tmp_path: Path) -> Path:
    """A fake wine binary at <root>/Proton X/bin/wine with winecfg + wineserver."""
    root = tmp_path / "Proton X"
    bin_dir = root / "files" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("wine", "winecfg", "wineserver"):
        p = bin_dir / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    return bin_dir / "wine"


def test_detect_prefix_arch(tmp_path: Path) -> None:
    # Not initialised -> None.
    assert detect_prefix_arch(str(tmp_path / "px")) is None

    # 64-bit marker.
    p64 = tmp_path / "p64"
    (p64 / "drive_c" / "windows" / "syswow64").mkdir(parents=True)
    (p64 / "system.reg").write_text("x")
    assert detect_prefix_arch(str(p64)) == "win64"

    # 32-bit (no syswow64).
    p32 = tmp_path / "p32"
    (p32 / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (p32 / "system.reg").write_text("x")
    assert detect_prefix_arch(str(p32)) == "win32"


def test_prepare_prefix_initialises_and_detects_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wine = _fake_wine(tmp_path)
    prefix = str(tmp_path / "pfx")

    # Empty prefix: prepares by running wineboot (mocked away).
    import subprocess

    calls: list[str] = []
    envs: list[dict] = []

    def _fake_run(*a, **k):
        calls.append(" ".join(a[0]))
        envs.append(k.get("env") or {})
        return _proc(0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    prepare_prefix(str(wine), prefix, steam_run=False)
    assert calls and "wineboot" in calls[0]
    assert envs and envs[0].get("WINEARCH") == "win64"
    assert envs[0].get("WINEPREFIX") == prefix

    # A 32-bit existing prefix cannot be used by our win64 runner.
    calls.clear()
    p32 = tmp_path / "p32"
    (p32 / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (p32 / "system.reg").write_text("x")
    with pytest.raises(ValueError, match="win32"):
        prepare_prefix(str(wine), str(p32), steam_run=False)


def test_open_winecfg_command(tmp_path: Path) -> None:
    wine = _fake_wine(tmp_path)
    cmd, env = open_winecfg_command(str(wine), "/home/x/pfx", steam_run=False)
    assert any("winecfg" in c for c in cmd)
    assert env["WINEPREFIX"] == "/home/x/pfx"


def _proc(code):
    class _P:
        returncode = code

    return _P()
