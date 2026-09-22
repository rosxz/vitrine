"""Tests for runtime GPU driver discovery (Vulkan/GL env injection)."""

from __future__ import annotations

import vitrine.gpu as gpu


def _fake_mesa(tmp_path, tag):
    """Create a mesa-like store dir with an Intel Vulkan driver + ICD json."""
    root = tmp_path / f"mesa-{tag}"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (lib / "libvulkan_intel.so").write_text("driver")
    icd = root / "share" / "vulkan" / "icd.d"
    icd.mkdir(parents=True)
    (icd / "intel_icd.x86_64.json").write_text(
        f'{{"ICD": {{"library_path": "{root}/libvulkan_intel.so", '
        '"api_version": "1.4.348"}}}'
    )
    (root / "lib" / "dri").mkdir(parents=True)
    (root / "lib" / "dri" / "iris_dri.so").write_text("gl")
    return root


def _fake_loader(tmp_path, tag="1.4.357"):
    lib = tmp_path / f"vulkan-loader-{tag}" / "lib"
    lib.mkdir(parents=True)
    real = lib / f"libvulkan.so.1.{tag}"
    real.write_text("loader")
    (lib / "libvulkan.so.1").symlink_to(real.name)
    return lib


def _set_ld(monkeypatch, value):
    monkeypatch.setattr(gpu.os.environ, "get", lambda k, d=None: value if k == "LD_LIBRARY_PATH" else d)


def _reset_cache(monkeypatch):
    monkeypatch.setattr(gpu, "_driver_cache", None)


def test_ready_prefers_x86_64_anv(tmp_path, monkeypatch):
    """Discover picks the dir with a real 64-bit Intel ICD, not i686."""
    _reset_cache(monkeypatch)
    mesa = _fake_mesa(tmp_path, "26.1.2")
    loader = _fake_loader(tmp_path)
    # Also add a decoy i686 icd that must NOT be selected.
    (mesa / "share" / "vulkan" / "icd.d" / "intel_icd.i686.json").write_text("{}")

    monkeypatch.setattr(gpu, "_driver_link_roots", lambda: [])
    monkeypatch.setattr(gpu, "_intel_mesa_dirs", lambda: [str(mesa / "lib")])
    monkeypatch.setattr(gpu, "_vulkan_loader_dir", lambda: str(loader))
    monkeypatch.setattr(gpu, "_glvnd_dir", lambda: None)
    _set_ld(monkeypatch, "")

    d = gpu.discover()
    assert d.ready
    assert d.mesa_lib == str(mesa / "lib")
    assert str(d.icd_json).endswith("intel_icd.x86_64.json")
    assert ".i686." not in d.icd_json
    assert d.vulkan_loader_lib == str(loader)
    assert d.icd_json is not None and str(mesa) in d.icd_json


def test_env_sets_vk_icd_and_ld_path(tmp_path, monkeypatch):
    """driver_env folds driver paths into VK_ICD_FILENAMES + LD_LIBRARY_PATH."""
    _reset_cache(monkeypatch)
    mesa = _fake_mesa(tmp_path, "26.1.2")
    loader = _fake_loader(tmp_path)
    monkeypatch.setattr(gpu, "_driver_link_roots", lambda: [])
    monkeypatch.setattr(gpu, "_intel_mesa_dirs", lambda: [str(mesa / "lib")])
    monkeypatch.setattr(gpu, "_vulkan_loader_dir", lambda: str(loader))
    monkeypatch.setattr(gpu, "_glvnd_dir", lambda: None)
    _set_ld(monkeypatch, "/old/lib")

    base = {"WINEPREFIX": "/p", "LD_LIBRARY_PATH": "/old/lib"}
    out = gpu.driver_env(base)
    assert out["WINEPREFIX"] == "/p"
    assert out["VK_ICD_FILENAMES"].endswith("intel_icd.x86_64.json")
    ld = out["LD_LIBRARY_PATH"]
    assert str(mesa / "lib") in ld
    assert str(loader) in ld
    assert "/old/lib" in ld  # existing preserved (not clobbered)
    assert out["LIBGL_DRIVERS_PATH"] == str(mesa / "lib" / "dri")


def test_env_budget_when_not_ready(monkeypatch):
    """If no driver is found, env is returned unchanged (no crash on fallback)."""
    _reset_cache(monkeypatch)
    fake = gpu.NixosDriverEnv()
    monkeypatch.setattr(gpu, "discover", lambda: fake)
    base = {"A": "1"}
    assert gpu.driver_env(base) == {"A": "1"}


def test_apply_gpu_env_is_driver_env(monkeypatch):
    """apply_gpu_env is an alias so callers get the same behaviour."""
    _reset_cache(monkeypatch)
    assert gpu.apply_gpu_env is gpu.driver_env