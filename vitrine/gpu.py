"""Runtime discovery of the active hardware GPU drivers (Vulkan/GL).

Vitrine launches Wine/Proton under ``steam-run`` (a bwrap FHS jail) and
optionally through ``gamescope``. On NixOS those spawned processes inherit the
app's ``LD_LIBRARY_PATH`` but get **no** Mesa/vulkan-loader/libGL paths, and the
bwrap FHS ``/usr`` doesn't expose an Intel ICD or ``libvulkan.so.1`` either.
Gamescope requires Vulkan to composite, and Wine needs GL/Vulkan to present, so
the driver-less environment yields an invisible/blank window.

This module locates the *running* Mesa/vulkan-loader/libglvnd store directories
(so the fix survives system rebuilds) and returns the env vars -- ``VK_ICD_FILENAMES``
and ``LD_LIBRARY_PATH`` entries -- needed for hardware Vulkan/GL to resolve.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field


@dataclass
class NixosDriverEnv:
    """The discovered driver paths and the env vars derived from them."""

    mesa_lib: str | None = None
    vulkan_loader_lib: str | None = None
    glvnd_lib: str | None = None
    icd_json: str | None = None
    dri_dir: str | None = None
    freetype_so: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when we found a usable Intel ICD and its loader."""
        return bool(self.icd_json and self.vulkan_loader_lib)

    def env(self, base: dict | None = None) -> dict:
        """Return a shallow copy of ``base`` with the GPU vars applied."""
        out = dict(base if base is not None else os.environ)
        if self.icd_json:
            out["VK_ICD_FILENAMES"] = self.icd_json
        libs: list[str] = []
        for d in (self.mesa_lib, self.vulkan_loader_lib, self.glvnd_lib):
            if d and d not in libs:
                libs.append(d)
        if self.dri_dir:
            out.setdefault("LIBGL_DRIVERS_PATH", self.dri_dir)
            out.setdefault("MESA_DRIVER_PATH", self.dri_dir)
        if libs:
            existing = out.get("LD_LIBRARY_PATH")
            merged = libs + ([existing] if existing else [])
            out["LD_LIBRARY_PATH"] = ":".join(x for x in merged if x)
        # Wine's wineloader ignores the caller's LD_LIBRARY_PATH for its runtime
        # dlopen of libfreetype (fonts). It *does* honour LD_PRELOAD, so preload
        # FreeType explicitly -- otherwise Proton GUIs render blank/no text.
        if self.freetype_so:
            existing_pre = out.get("LD_PRELOAD")
            out["LD_PRELOAD"] = ":".join(
                x for x in (self.freetype_so, existing_pre) if x
            )
        return out


def _inactive_store(inputs: list[str]) -> list[str]:
    """Filter out store paths that are already wired into the environment."""
    ld = os.environ.get("LD_LIBRARY_PATH", "")
    active = set(filter(None, ld.split(":")))
    return [x for x in inputs if x not in active]


def discover() -> NixosDriverEnv:
    """Scan the nix store / system profile for active Mesa+loader paths.

    Picks a Mesa that ships a working **mainline** Intel Vulkan driver
    (``intel_icd``/ANV, ``libvulkan_intel.so``) — the ``intel_hasvk`` driver does
    not initialise on many desktop GPUs. Prefers a mesa referenced by the running
    system profile and a vulkan-loader that actually provides ``libvulkan.so.1``.
    """
    out = NixosDriverEnv()
    intel_sources = _intel_mesa_dirs()

    # Prefer the mesa that the live system driver link points at.
    link_mesa = _mesa_from_driver_root()
    if link_mesa and _has_anv(link_mesa):
        out.mesa_lib = link_mesa
        out.icd_json = _find_icd(os.path.dirname(link_mesa), "intel_icd")

    # Fallback: any mesa with a mainline Intel driver (put 64-bit first).
    if out.mesa_lib is None:
        for libdir in _prefer_x86_64(intel_sources):
            if _has_anv(libdir):
                out.mesa_lib = libdir
                out.icd_json = _find_icd(os.path.dirname(libdir), "intel_icd")
                break

    if out.mesa_lib:
        dri = os.path.join(os.path.dirname(out.mesa_lib), "lib", "dri")
        if not os.path.isdir(dri):
            dri = os.path.join(out.mesa_lib, "dri")
        if os.path.isdir(dri):
            out.dri_dir = dri

    out.vulkan_loader_lib = _vulkan_loader_dir()
    out.glvnd_lib = _glvnd_dir()
    out.freetype_so = _freetype_so()
    return out


def _has_anv(libdir: str) -> bool:
    """True when ``libdir`` holds the mainline Intel Vulkan driver + manifest."""
    if not os.path.isfile(os.path.join(libdir, "libvulkan_intel.so")):
        return False
    return _find_icd(os.path.dirname(libdir), "intel_icd") is not None


def _intel_mesa_dirs() -> list[str]:
    """All nix-store mesa lib dirs that ship the Intel Vulkan driver."""
    dirs: list[str] = []
    for pattern in ("/nix/store/*mesa-*/lib", "/nix/store/*mesa-*-drivers/lib"):
        for d in glob.glob(pattern):
            if os.path.isfile(os.path.join(d, "libvulkan_intel.so")):
                dirs.append(d)
    return dirs


def _driver_link_roots() -> list[str]:
    """Real (readlink-resolved) store dirs that the ``/run/opengl-driver*`` links
    point at, along with the live system profile lib dir."""
    roots: list[str] = []
    for link in ("/run/opengl-driver", "/run/opengl-driver-32"):
        if os.path.islink(link):
            try:
                roots.append(os.path.realpath(link))
            except OSError:  # pragma: no cover
                pass
    prof = _resolve_profile_lib("libEGL_mesa.so.0")
    if prof:
        roots.append(os.path.dirname(os.path.dirname(prof)))
    return roots


def _mesa_from_driver_root() -> str | None:
    """Real mesa lib dir that the live driver link's ``libvulkan_intel.so``
    points at (NixOS symlinkJoin resolves to the actual mesa store path)."""
    for root in _driver_link_roots():
        for name in ("libvulkan_intel.so", "libEGL_mesa.so.0", "libGLX_mesa.so.0"):
            cand = os.path.join(root, "lib", name)
            if os.path.islink(cand) or os.path.exists(cand):
                target = os.path.realpath(cand)
                if not os.path.exists(target):
                    continue
                libdir = os.path.dirname(target)
                if os.path.isfile(os.path.join(libdir, "libvulkan_intel.so")):
                    return libdir
                parent = os.path.dirname(libdir)
                if os.path.isfile(os.path.join(parent, "lib", "libvulkan_intel.so")):
                    return os.path.join(parent, "lib")
        return None
    return None


def _resolve_profile_lib(name: str) -> str | None:
    """Resolve ``lib/<name>`` from the running system profile, if present."""
    for base in ("/run/current-system/sw/lib", "/etc/profiles/*/lib"):
        for path in sorted(glob.glob(base)):
            if not os.path.isdir(path):
                continue
            candidate = os.path.join(path, name)
            if os.path.exists(candidate):
                try:
                    return os.path.realpath(candidate)
                except OSError:  # pragma: no cover
                    return candidate
    return None


def _prefer_x86_64(dirs: list[str]) -> list[str]:
    """Order dirs so 64-bit-only mesa (no i686 icd) come first."""

    def _score(d: str) -> int:
        icd = _find_icd(os.path.dirname(d), "intel_icd")
        if icd is None:
            return 2
        return 0 if ".i686." not in icd else 1

    return sorted(dirs, key=_score)


def _find_icd(mesa_root: str, match: str) -> str | None:
    """Return ``share/vulkan/icd.d/<match>*.json`` preferring x86_64 over i686.

    Mesa ships both ``intel_icd.x86_64.json`` and ``intel_icd.i686.json``; the
    desktop (and gamescope) are 64-bit, so prefer an explicitly-64-bit or
    arch-less manifest and never a 32-bit one.
    """
    base = os.path.join(mesa_root, "share", "vulkan", "icd.d")
    if not os.path.isdir(base):
        return None
    matches = sorted(glob.glob(os.path.join(base, f"{match}*.json")))
    if not matches:
        return None
    plain = [m for m in matches if ".x86_64." not in m and ".i686." not in m]
    x86_64 = sorted(m for m in matches if ".x86_64." in m)
    picks = plain or x86_64 or matches
    return picks[0]


def _vulkan_loader_dir() -> str | None:
    """Locate a real ``libvulkan.so.1`` provider in the store."""
    for d in glob.glob("/nix/store/*vulkan-loader-*/lib"):
        real = _real_libso(d, "libvulkan.so.1")
        if real and os.path.isdir(real):
            return real
    return None


def _real_libso(libdir: str, name: str) -> str | None:
    """Return the real store dir for ``<libdir>/<name>`` if it truly exists."""
    candidate = os.path.join(libdir, name)
    if not os.path.exists(candidate):
        return None
    target = os.path.realpath(candidate)
    return os.path.dirname(target) if os.path.exists(target) else libdir


def _glvnd_dir() -> str | None:
    """Locate a ``libGL.so.1`` (libglvnd) provider."""
    prof = _resolve_profile_lib("libGL.so.1")
    if prof:
        return os.path.dirname(prof)
    for d in glob.glob("/nix/store/*libglvnd-*/lib"):
        if os.path.exists(os.path.join(d, "libGL.so.1")):
            return d
    return None


def _freetype_so() -> str | None:
    """Locate a real ``libfreetype.so.6`` to LD_PRELOAD for Proton's wineloader.

    Proton's ``win32u.so`` dlopens FreeType at runtime to draw fonts. Its custom
    wineloader ignores the caller's ``LD_LIBRARY_PATH`` but honours ``LD_PRELOAD``,
    so Vitrine preloads the installed FreeType so GUI text renders. Without it,
    Proton GUIs (winecfg, games) draw frames but no glyphs -- invisible text.
    """
    prof = _resolve_profile_lib("libfreetype.so.6")
    if prof and os.path.exists(prof):
        return prof
    seen: set[str] = set()
    for d in glob.glob("/nix/store/*freetype-*/lib"):
        candidate = os.path.join(d, "libfreetype.so.6")
        if not os.path.exists(candidate):
            continue
        target = os.path.realpath(candidate)
        if target in seen:
            continue
        seen.add(target)
        if os.path.exists(target):
            return target
    return None


_driver_cache: NixosDriverEnv | None = None


def driver_env(base: dict | None = None) -> dict:
    """Apply the discovered driver env onto ``base`` (cached discovery)."""
    global _driver_cache
    if _driver_cache is None:
        _driver_cache = discover()
    return _driver_cache.env(base)


apply_gpu_env = driver_env