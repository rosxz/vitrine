"""Install Microsoft D3D runtime DLLs (``d3d_extras``) into a Wine prefix.

Many old Windows games (e.g. Super Meat Boy) fail to start under Wine because
they depend on the DirectX 9 runtime DLLs that Wine does not bundle --
``d3dx9_43.dll``, ``d3dcompiler_43.dll`` and friends. Lutris ships these in its
``d3d_extras`` release (``github.com/lutris/d3d_extras``), which provides 32-bit
and 64-bit copies of the DLLs.

Vitrine bundles that archive (via the flake, mirroring legendary/gogdl) and,
before launching a game, copies the needed DLLs into the prefix's ``system32``
(64-bit) and ``syswow64`` (32-bit) directories and marks them ``native`` through
``WINEDLLOVERRIDES`` so Wine loads our copies instead of its incomplete builtins.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: DLLs provided by d3d_extras (both arch dirs). We install all of them so any
#: game that needs a DirectX 9/10/11 helper DLL finds it.
MANAGED_DLLS = (
    "d3dx10",
    "d3dx10_33",
    "d3dx10_34",
    "d3dx10_35",
    "d3dx10_36",
    "d3dx10_37",
    "d3dx10_38",
    "d3dx10_39",
    "d3dx10_40",
    "d3dx10_41",
    "d3dx10_42",
    "d3dx10_43",
    "d3dx11_42",
    "d3dx11_43",
    "d3dx9_24",
    "d3dx9_25",
    "d3dx9_26",
    "d3dx9_27",
    "d3dx9_28",
    "d3dx9_29",
    "d3dx9_30",
    "d3dx9_31",
    "d3dx9_32",
    "d3dx9_33",
    "d3dx9_34",
    "d3dx9_35",
    "d3dx9_36",
    "d3dx9_37",
    "d3dx9_38",
    "d3dx9_39",
    "d3dx9_40",
    "d3dx9_41",
    "d3dx9_42",
    "d3dx9_43",
    "d3dcompiler_33",
    "d3dcompiler_34",
    "d3dcompiler_35",
    "d3dcompiler_36",
    "d3dcompiler_37",
    "d3dcompiler_38",
    "d3dcompiler_39",
    "d3dcompiler_40",
    "d3dcompiler_41",
    "d3dcompiler_42",
    "d3dcompiler_43",
    "d3dcompiler_46",
    "d3dcompiler_47",
)

#: Environment override for the bundled d3d_extras archive root (e.g. the flake
#: sets VITRINE_D3D_EXTRAS to the unpacked directory).
D3D_EXTRAS_ENV = "VITRINE_D3D_EXTRAS"


class D3DExtrasError(Exception):
    """Raised when the d3d_extras bundle is unavailable."""


def extras_root() -> str | None:
    """Return the unpacked d3d_extras archive root, or ``None`` if unavailable.

    The root is the directory that directly contains ``x32/`` and ``x64/``.
    """
    override = os.environ.get(D3D_EXTRAS_ENV)
    if override:
        return override if os.path.isdir(override) else None
    # Locate it under the data dir as a fallback (runtime download).
    from .. import paths

    candidate = paths.data_dir() / "d3d_extras"
    return str(candidate) if os.path.isdir(candidate) else None


def is_available() -> bool:
    root = extras_root()
    return bool(root and os.path.isdir(os.path.join(root, "x32")) and os.path.isdir(os.path.join(root, "x64")))


def install_to_prefix(prefix: str) -> set[str]:
    """Copy the managed DLLs into ``prefix``'s ``system32``/``syswow64`` dirs.

    Idempotent: skips DLLs already present. Returns the set of DLL names that
    were installed (and should be marked ``native`` in WINEDLLOVERRIDES).
    """
    root = extras_root()
    if not root:
        raise D3DExtrasError(f"d3d_extras is not available (set {D3D_EXTRAS_ENV})")

    drive_c = Path(prefix) / "drive_c" / "windows"
    system32 = drive_c / "system32"
    syswow64 = drive_c / "syswow64"
    x64 = Path(root) / "x64"
    x32 = Path(root) / "x32"

    installed: set[str] = set()
    for dll in MANAGED_DLLS:
        dll_file = f"{dll}.dll"
        # 64-bit DLL -> system32
        src64 = x64 / dll_file
        if src64.is_file():
            system32.mkdir(parents=True, exist_ok=True)
            dst = system32 / dll_file
            if not dst.exists():
                _copy_file(src64, dst)
            installed.add(dll)
        # 32-bit DLL -> syswow64
        src32 = x32 / dll_file
        if src32.is_file():
            syswow64.mkdir(parents=True, exist_ok=True)
            dst = syswow64 / dll_file
            if not dst.exists():
                _copy_file(src32, dst)
            installed.add(dll)
    return installed


def dll_overrides(enabled: set[str] | None = None) -> str:
    """Return a ``WINEDLLOVERRIDES`` string marking the d3d_extras DLLs native.

    ``enabled`` is the set returned by :func:`install_to_prefix`; if omitted all
    managed DLLs are marked native (harmless for those not present).
    """
    dlls = sorted(enabled if enabled is not None else MANAGED_DLLS)
    return ";".join(f"{dll}=n" for dll in dlls)


def _copy_file(src: Path, dst: Path) -> None:
    # Prefer a symlink when the source lives under the (immutable) nix store;
    # fall back to a real copy if the engine can't create links.
    try:
        os.symlink(str(src), str(dst))
    except OSError:  # pragma: no cover - e.g. cross-device/permission issues
        shutil.copy2(src, dst)