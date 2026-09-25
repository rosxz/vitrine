"""Wine prefix preparation and architecture compatibility.

A Wine prefix must be created with the same ``WINEARCH`` as the wine binary that
will use it. Running a 64-bit Proton wine on a 32-bit prefix (or vice versa) is
incompatible -- Wine cannot silently convert an existing prefix's architecture.
This module detects the prefix's architecture, ensures it matches the runner,
initialises a fresh prefix with ``wineboot`` so it is ready before the game
launches, and surfaces a clear error when an existing prefix's architecture
cannot be matched without a full rebuild.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .library import Game, Library

logger = logging.getLogger(__name__)

#: Markers that indicate a 64-bit Windows prefix.
_64BIT_MARKERS = ("drive_c/windows/syswow64",)


def _prefix_root(prefix: str) -> Path:
    return Path(os.path.expanduser(prefix))


def detect_prefix_arch(prefix: str) -> str | None:
    """Return the prefix's architecture ("win64"|"win32") or ``None`` if it
    does not exist yet."""
    root = _prefix_root(prefix)
    if not (root / "system.reg").is_file():
        return None  # not initialised yet
    # A 64-bit prefix contains syswow64 (32-bit compatibility) alongside
    # system32; a 32-bit prefix has only system32.
    for marker in _64BIT_MARKERS:
        if (root / marker).exists():
            return "win64"
    return "win32"


def desired_arch(wine_binary: str) -> str:
    """The architecture a wine binary expects (win32/win64).

    Best-effort: Proton and 64-bit builds want win64. Anything else defaults to
    win64 as well -- almost every modern game is 64-bit and Wine's default is
    win64 when WINEARCH is unset.
    """
    return "win64"


def prepare_prefix(
    wine_binary: str,
    prefix: str,
    *,
    steam_run: bool = False,
) -> None:
    """Initialise ``prefix`` for ``wine_binary`` if it is empty or has a
    mismatched architecture.

    Creates the prefix directory, runs ``wineboot`` (inside ``steam-run`` when
    set) so the prefix is ready before a game launches. Raises ``ValueError``
    if an existing prefix's architecture does not match the runner; in that
    case the user should delete the prefix so it is recreated correctly.
    """
    root = _prefix_root(prefix)
    if root.exists():
        stop_wineserver(wine_binary, prefix, steam_run=steam_run)
    root.mkdir(parents=True, exist_ok=True)

    # A half-initialised prefix (registry present but DLLs missing) is the
    # broken "could not load kernel32.dll" state. Wine refuses to finish
    # bootstrapping an existing-but-incomplete prefix, so clear it completely
    # and let it be recreated fresh. Handle this before the arch check -- a
    # broken prefix may report a misleading architecture.
    if _kernel32_missing(root):
        logger.warning("Rebuilding incomplete prefix %s (clearing %s)", prefix, root)
        del_existing(root)

    existing = detect_prefix_arch(prefix)
    required = desired_arch(wine_binary)
    if existing is not None and existing != required:
        raise ValueError(
            f"Prefix {prefix} is a {existing} prefix but the selected runner needs "
            f"{required}. Delete the prefix so Vitrine can recreate it for this runner."
        )

    # If the prefix already exists and matches, nothing to do.
    if not _needs_boot(root):
        return

    # Proton prefixes must be seeded from Proton's own ``default_pfx`` (a fully
    # prepared prefix containing the D3D/vkd3d/dxvk runtime DLLs Wine doesn't
    # install via wineboot). Bare wineboot yields a prefix missing those, and
    # D3D9/11 games then fail with 'libvkd3d-1.dll not found'. Mirror Proton's
    # first-run setup: copy default_pfx, preserving builtin DLL symlinks.
    if _seed_from_proton(wine_binary, root):
        logger.info("Seeded Proton prefix %s", prefix)
        return

    _run_wineboot(wine_binary, prefix, steam_run=steam_run)


def recreate_prefix(
    wine_binary: str,
    prefix: str,
    *,
    steam_run: bool = False,
) -> None:
    """Delete and freshly prepare a game's prefix for the selected runner."""
    stop_wineserver(wine_binary, prefix, steam_run=steam_run)
    shutil.rmtree(_prefix_root(prefix), ignore_errors=True)
    prepare_prefix(wine_binary, prefix, steam_run=steam_run)


def recreate_prefix_for_game(
    game: Game,
    library: Library,
    *,
    on_started: Callable[[], None],
    on_finished: Callable[[Exception | None], None],
) -> None:
    """Resolve a game's runner on GTK's thread and rebuild its prefix in a worker."""
    from .launch import wine_prefix_for
    from .runners import DEFAULT_PROTON_SETTING, get_runner, load_runners_store, resolve_runner

    try:
        config = game.merged_config(library.global_config())
        store = load_runners_store(library)
        runner_id = (
            game.config.get("runner")
            or library.setting(DEFAULT_PROTON_SETTING, None)
            or config.get("runner")
        )
        wine_binary = resolve_runner(runner_id, store, config.get("wine_binary"))
        runner = get_runner(runner_id, store)
        prefix = str(wine_prefix_for(game))
        steam_run = runner is not None and runner.kind == "proton"
    except Exception as exc:  # noqa: BLE001 - return lookup failures to the UI
        on_finished(exc)
        return

    on_started()

    def rebuild() -> None:
        try:
            recreate_prefix(wine_binary, prefix, steam_run=steam_run)
        except Exception as exc:  # noqa: BLE001 - return preparation failures to the UI
            logger.exception("recreating prefix for %s failed", game.name)
            on_finished(exc)
            return
        on_finished(None)

    threading.Thread(target=rebuild, daemon=True, name="vitrine-prefix-recreate").start()


def configure_wine_environment(env: dict[str, str], wine_binary: str) -> dict[str, str]:
    """Pin Wine to a 64-bit prefix and the selected runner's wineserver."""
    env["WINEARCH"] = "win64"
    wineserver = _siblings_binary(wine_binary, "wineserver")
    if wineserver:
        env["WINESERVER"] = wineserver
    return env


def stop_wineserver(wine_binary: str, prefix: str, *, steam_run: bool = False) -> None:
    """Stop the wineserver associated with ``prefix``, if the runner provides one."""
    wineserver = _siblings_binary(wine_binary, "wineserver")
    if not wineserver:
        return
    env = configure_wine_environment(
        {**os.environ, "WINEPREFIX": os.path.expanduser(prefix)}, wine_binary
    )
    try:
        subprocess.run(
            ["steam-run", wineserver, "-k"] if steam_run else [wineserver, "-k"],
            env=env,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not stop existing wineserver for %s: %s", prefix, exc)


def _proton_default_pfx(wine_binary: str) -> Path | None:
    """The Proton distribution's ``share/default_pfx`` if ``wine_binary`` is a
    Proton build that ships one, else ``None``.

    Proton keeps a ready-made prefix at ``<dist>/files/share/default_pfx`` used
    to seed a fresh game prefix on first run. The wine binary lives at
    ``<dist>/files/bin/wine``.
    """
    binary = Path(os.path.expanduser(wine_binary))
    candidates = (
        binary.parent.parent / "share" / "default_pfx",
        binary.parent.parent / "files" / "share" / "default_pfx",
    )
    for candidate in candidates:
        if (candidate / "system.reg").is_file():
            return candidate
    return None


def _seed_from_proton(wine_binary: str, root: Path) -> bool:
    """Seed ``root`` from Proton's ``default_pfx`` if available.

    Mirrors Proton's ``copy_pfx``: recursively copy every file, re-aiming Wine
    builtin DLL symlinks at the Proton dist's ``lib/wine`` (absolute, so they
    survive being moved out of the dist), create the DOS drive mappings
    (``c:`` -> ``drive_c``, ``z:`` -> ``/`` ; default_pfx ships an empty
    ``dosdevices/``), and stamp ``.update-timestamp`` so Wine doesn't try to
    re-update the prefix. Returns True when seeding worked.
    """
    default_pfx = _proton_default_pfx(wine_binary)
    if default_pfx is None or not default_pfx.is_dir():
        return False
    # The Proton dist root = default_pfx/../.. (share/default_pfx -> <dist>/share/...)
    dist = default_pfx.parent.parent
    try:
        _copy_tree_preserving_links(default_pfx, root, dist)
        _create_dos_drives(root)
        # Mirror Proton: stamp .update-timestamp from the installed wine.inf so
        # Wine leaves the seeded prefix alone.
        inf = default_pfx.parent / "wine" / "wine.inf"
        mtime = int(inf.stat().st_mtime) if inf.is_file() else 0
        (root / ".update-timestamp").write_text(str(mtime))
        return True
    except OSError as exc:  # noqa: BLE001 - fall back to wineboot on any failure
        logger.warning("Could not seed Proton prefix from %s: %s", default_pfx, exc)
        return False


def _create_dos_drives(root: Path) -> None:
    """Create the DOS drive mappings Wine needs under ``dosdevices/``.

    A fresh (or default_pfx-seeded) prefix has an empty ``dosdevices/``; without
    ``c:`` (-> drive_c) and ``z:`` (-> /) Wine cannot map paths or load system
    DLLs. Mirrors Proton's own first-run setup.
    """
    dos = root / "dosdevices"
    dos.mkdir(parents=True, exist_ok=True)
    c_link = dos / "c:"
    if not c_link.exists() and not c_link.is_symlink():
        c_link.symlink_to("../drive_c")
    z_link = dos / "z:"
    if not z_link.exists() and not z_link.is_symlink():
        z_link.symlink_to("/")


def _copy_tree_preserving_links(src: Path, dst: Path, dist: Path) -> None:
    """Recursive copy where Wine builtin DLL symlinks are re-aimed at the dist.

    Proton's ``default_pfx`` stores most system DLLs as *relative* symlinks into
    ``<dist>/lib/wine/<arch>-windows/<name>.dll``. Simply recreating those links
    elsewhere breaks them (they'd resolve against the new prefix location). Like
    Proton's ``pfx_copy``, detect a builtin link and rewrite it as an *absolute*
    path into the real dist ``lib/wine``. Ordinary files are copied literally.
    """
    import shutil as _sh

    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_symlink():
            contents = os.readlink(entry)
            resolved = os.path.normpath(os.path.join(entry.parent, contents))
            # A builtin DLL symlink points inside <dist>/lib/wine/*-windows/.
            is_builtin = "/lib/wine/" in str(resolved)
            if is_builtin and Path(resolved).is_relative_to(dist):
                rel = Path(resolved).relative_to(dist / "lib" / "wine")
                target.symlink_to(str(dist / "lib" / "wine" / rel))
            else:
                # Non-builtin symlink: preserve the relative target as-is.
                target.symlink_to(contents)
        elif entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree_preserving_links(entry, target, dist)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(entry, target)


def del_existing(root: Path) -> None:
    """Remove all contents of the prefix dir, leaving an empty prefix dir."""
    import shutil as _sh

    for entry in list(root.iterdir()):
        if entry.is_dir():
            _sh.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _needs_boot(root: Path) -> bool:
    """True when the prefix is fresh (no wineboot output yet)."""
    return not (root / "drive_c" / "windows" / "system32").is_dir()


def _kernel32_missing(root: Path) -> bool:
    """True when the prefix is half-initialised (registry written but no DLLs)."""
    return (root / "system.reg").is_file() and not (
        root / "drive_c" / "windows" / "system32" / "kernel32.dll"
    ).exists()


def _run_wineboot(wine_binary: str, prefix: str, *, steam_run: bool) -> None:
    """Run ``wineboot`` on the (fresh) prefix to initialise it."""
    from .gpu import driver_env

    env = dict(os.environ)
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    configure_wine_environment(env, wine_binary)
    env["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d"
    env = driver_env(env)
    wineserver = _siblings_binary(wine_binary, "wineserver")
    command = [wine_binary, "wineboot", "-i"]
    if steam_run and shutil.which("steam-run"):
        command = ["steam-run", *command]
    logger.info("Preparing prefix %s with %s", prefix, wine_binary)
    try:
        subprocess.run(command, env=env, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("prefix init for %s failed: %s", prefix, exc)
    if wineserver:
        try:
            subprocess.run(["steam-run", wineserver, "-w"] if steam_run else [wineserver, "-w"], env=env, timeout=60)
        except Exception:  # noqa: BLE001 - waitserver cleanup is best-effort
            pass


def _siblings_binary(wine_binary: str, name: str) -> str | None:
    """Return a sibling binary (e.g. wineserver) next to the wine executable."""
    bin_dir = Path(os.path.expanduser(wine_binary)).parent
    candidate = bin_dir / name
    return str(candidate) if candidate.is_file() else None


def open_winecfg_command(
    wine_binary: str,
    prefix: str,
    *,
    steam_run: bool = False,
) -> list[str]:
    """Command to open the runner's Wine configuration (winecfg) for ``prefix``.

    Wine ships a built-in ``winecfg`` program run as ``wine winecfg``; there is
    usually no standalone ``winecfg`` binary next to the wine executable (Proton
    ships none). So invoke ``wine winecfg`` (or the standalone sibling if one
    exists), wrapped in steam-run for Proton.
    """
    winecfg = _siblings_binary(wine_binary, "winecfg")
    if winecfg:
        command = [winecfg]
    else:
        command = [wine_binary, "winecfg"]
    if steam_run and shutil.which("steam-run"):
        command = ["steam-run", *command]
    env = dict(os.environ)
    env["WINEPREFIX"] = os.path.expanduser(prefix)
    env["WINEARCH"] = "win64"
    _ensure_library_path(env, ["/lib", "/lib64", "/usr/lib", "/usr/lib64"])
    from .gpu import driver_env

    env = driver_env(env)
    return command, env


def _ensure_library_path(env: dict, dirs: list[str]) -> None:
    """Prepend existing dirs to LD_LIBRARY_PATH so nested dls (wine's freetype)
    resolve. steam-run's bwrap FHS mounts libraries under /lib but does not add
    it to the loader search path, so Wine's own dlopen("libfreetype.so.*") fails
    and GUIs render frames with no fonts.
    """
    existing = env.get("LD_LIBRARY_PATH")
    present = [d for d in dirs if os.path.isdir(d)]
    merged = present + ([existing] if existing else [])
    env["LD_LIBRARY_PATH"] = ":".join(d for d in merged if d)