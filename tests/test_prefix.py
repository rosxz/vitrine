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

    # A 32-bit existing prefix cannot be used by our win64 runner. Give it a
    # kernel32.dll marker so it looks fully bootstrapped (not split-init).
    calls.clear()
    p32 = tmp_path / "p32"
    kern = p32 / "drive_c" / "windows" / "system32" / "kernel32.dll"
    kern.parent.mkdir(parents=True)
    kern.write_text("x")
    (p32 / "system.reg").write_text("x")
    with pytest.raises(ValueError, match="win32"):
        prepare_prefix(str(wine), str(p32), steam_run=False)


def test_prepare_rebuilds_half_initialised_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix with registry but no DLLs is rebuilt (kernel32 present)."""
    wine = _fake_wine(tmp_path)
    prefix = str(tmp_path / "broken")
    root = Path(prefix)
    (root / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (root / "system.reg").write_text("x")  # registry present, DLLs missing
    (root / "useless.db").write_text("x")

    import subprocess

    calls: list[str] = []
    envs: list[dict] = []

    def _fake_run(*a, **k):
        calls.append(" ".join(a[0]))
        envs.append(k.get("env") or {})
        return _proc(0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    prepare_prefix(str(wine), prefix, steam_run=False)

    # Whole dir was cleared, then wineboot ran fresh.
    assert not (root / "useless.db").exists()
    assert any("wineboot" in c for c in calls)
    assert envs[0].get("WINEARCH") == "win64"


def test_open_winecfg_command(tmp_path: Path) -> None:
    wine = _fake_wine(tmp_path)
    # The fake wine has a sibling winecfg -> uses it directly.
    cmd, env = open_winecfg_command(str(wine), "/home/x/pfx", steam_run=False)
    assert any("winecfg" in c for c in cmd)
    assert env["WINEPREFIX"] == "/home/x/pfx"

    # No sibling winecfg (e.g. Proton) -> `wine winecfg`.
    cmd2, _ = open_winecfg_command("/tmp/nosib/Proton/files/bin/wine", "/home/x/pfx", steam_run=True)
    assert cmd2[:2] == ["steam-run", "/tmp/nosib/Proton/files/bin/wine"]
    assert "winecfg" in cmd2


def _proc(code):
    class _P:
        returncode = code

    return _P()


def _fake_proton(tmp_path: Path) -> tuple[Path, Path]:
    """A fake Proton dist: <root>/files/bin/wine + a seeded default_pfx with a
    builtin-dll symlink pointing into <root>/files/lib/wine and a normal file."""
    root = tmp_path / "Proton 11"
    files = root / "files"
    (files / "bin").mkdir(parents=True)
    (files / "bin" / "wine").write_text("#!/bin/sh\n")
    (files / "bin" / "wine").chmod(0o755)

    libwin = files / "lib" / "wine" / "x86_64-windows"
    libwin.mkdir(parents=True)
    dll = libwin / "kernel32.dll"
    dll.write_text("dll-data")

    pfx = files / "share" / "default_pfx"
    sys32 = pfx / "drive_c" / "windows" / "system32"
    sys32.mkdir(parents=True)
    (sys32 / "kernel32.dll").symlink_to("../../../../../lib/wine/x86_64-windows/kernel32.dll")
    (sys32 / "depth.txt").write_text("real-file")
    (pfx / "system.reg").write_text("reg")
    inf = files / "share" / "wine" / "wine.inf"
    inf.parent.mkdir(parents=True)
    inf.write_text("inf")
    return root, files / "bin" / "wine"


def test_proton_default_pfx_located(tmp_path: Path) -> None:
    from vitrine.prefix import _proton_default_pfx

    root, wine = _fake_proton(tmp_path)
    dp = _proton_default_pfx(str(wine))
    assert dp == root / "files" / "share" / "default_pfx"


def test_seed_from_proton_absolutizes_builtin_symlinks(tmp_path: Path) -> None:
    from vitrine.prefix import _seed_from_proton

    root, wine = _fake_proton(tmp_path)
    target = tmp_path / "prefix"
    assert _seed_from_proton(str(wine), target) is True

    k = target / "drive_c" / "windows" / "system32" / "kernel32.dll"
    assert k.is_symlink()
    # Symlink now points at the real dist dll (not the broken relative path).
    assert k.exists()
    assert k.readlink().is_absolute()
    assert str(k.readlink()).startswith(str(root))
    # Ordinary files are copied.
    assert (target / "drive_c" / "windows" / "system32" / "depth.txt").read_text() == "real-file"
    # .update-timestamp is stamped with the wine.inf mtime.
    assert (target / ".update-timestamp").exists()
    # DOS drive mappings are created so Wine can resolve paths / load DLLs.
    assert (target / "dosdevices" / "z:").is_symlink()
    assert (target / "dosdevices" / "c:").is_symlink()


def test_seed_returns_false_without_proton_pfx(tmp_path: Path) -> None:
    from vitrine.prefix import _seed_from_proton

    wine = _fake_wine(tmp_path)  # plain wine layout, no default_pfx
    assert _seed_from_proton(str(wine), tmp_path / "prefix") is False
