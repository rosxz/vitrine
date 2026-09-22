"""Tests for DirectX runtime DLL installation into Wine prefixes."""

from __future__ import annotations

from pathlib import Path

import pytest

from vitrine.wine import d3d_extras


@pytest.fixture
def extras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake d3d_extras bundle root with x32/x64 dirs."""
    root = tmp_path / "d3d_extras"
    for arch, dlls in (("x64", ("d3dx9_43", "d3dcompiler_47")), ("x32", ("d3dx9_43", "d3dcompiler_43"))):
        d = root / arch
        d.mkdir(parents=True)
        for name in dlls:
            (d / f"{name}.dll").write_text(arch)
    monkeypatch.setenv(d3d_extras.D3D_EXTRAS_ENV, str(root))
    return root


def test_is_available_and_root(extras: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert d3d_extras.is_available()
    assert d3d_extras.extras_root() == str(extras)


def test_install_copies_into_system32_and_syswow64(tmp_path: Path, extras: Path) -> None:
    prefix = str(tmp_path / "pfx")
    installed = d3d_extras.install_to_prefix(prefix)

    assert "d3dx9_43" in installed
    assert "d3dcompiler_47" in installed
    assert "d3dcompiler_43" in installed

    # 64-bit DLL -> system32
    assert (tmp_path / "pfx/drive_c/windows/system32/d3dcompiler_47.dll").exists()
    # 32-bit DLL -> syswow64
    assert (tmp_path / "pfx/drive_c/windows/syswow64/d3dcompiler_43.dll").exists()


def test_install_is_idempotent(tmp_path: Path, extras: Path) -> None:
    prefix = str(tmp_path / "pfx")
    first = d3d_extras.install_to_prefix(prefix)
    second = d3d_extras.install_to_prefix(prefix)
    assert second == first
    assert (tmp_path / "pfx/drive_c/windows/system32/d3dcompiler_47.dll").exists()


def test_unavailable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(d3d_extras.D3D_EXTRAS_ENV, raising=False)
    with pytest.raises(d3d_extras.D3DExtrasError):
        d3d_extras.install_to_prefix(str(tmp_path / "p"))


def test_dll_overrides_fragment() -> None:
    frag = d3d_extras.dll_overrides({"d3dx9_43", "d3dcompiler_43"})
    assert "d3dx9_43=n" in frag
    assert "d3dcompiler_43=n" in frag
    # Sorted + semicolon joined.
    assert frag.count(";") == 1