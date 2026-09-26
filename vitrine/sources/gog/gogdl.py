"""Bridge to ``gogdl`` (Heroic's GOG depot/downloader) for depot installs.

Lutris and Heroic both prefer downloading GOG games via the depot/CDN manifest
rather than running the interactive offline installer. gogdl downloads the game
files straight into a target directory, writes a ``goggame-<id>.info`` manifest,
and lets us read back the executable from ``import``. That makes installs
non-interactive, deterministic, and the installed state reliable.

gogdl needs an auth-config JSON keyed by the GOG client id, with
``access_token``/``refresh_token``/``expires_in``/``loginTime``. Vitrine's
:class:`GogTokenStore` holds those (the login dialog persists the full token
payload); this module writes the gogdl config and builds download commands.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterable

from .auth import GOG_CLIENT_ID, GogTokenStore

#: Environment override for the bundled gogdl, mirroring VITRINE_LEGENDARY.
GOGDL_ENV = "VITRINE_GOGDL"
NOTHING_TO_DO = "Nothing to do"
GOGDL_CONFIG_ENV = "GOGDL_CONFIG_PATH"


class GogdlError(Exception):
    """Raised when gogdl is missing or fails."""


def reported_nothing_to_do(output: Iterable[str]) -> bool:
    """Return whether gogdl reported that a previous download is complete."""
    return any(line.strip() == f"{NOTHING_TO_DO}." for line in output)


def gogdl_binary() -> str:
    """Return the gogdl executable path, or raise if not installed."""
    override = os.environ.get(GOGDL_ENV)
    if override:
        return override
    path = shutil.which("gogdl")
    if not path:
        raise GogdlError(
            "gogdl is not installed. Install it (e.g. your distro's 'gogdl' package) "
            "and put 'gogdl' on PATH, or set VITRINE_GOGDL."
        )
    return path


def is_installed() -> bool:
    try:
        gogdl_binary()
        return True
    except GogdlError:
        return False


def write_auth_config(store: GogTokenStore, config_path: str) -> str:
    """Write Vitrine's GOG token into gogdl's expected auth-config format.

    gogdl flags a credential expired when ``now >= loginTime + expires_in``. The
    caller (GogSource.ensure_fresh_token) must refresh the token first; here we
    record ``loginTime`` as *now* with the token's actual lifetime so gogdl uses
    the fresh access_token directly instead of attempting (and possibly failing)
    its own refresh. Callers should pass a server-refreshed ``loginTime`` when
    one is known.
    """
    fetched_at = store.fetched_at() or int(time.time())
    stored_expires = store.expires_in()
    expires_in = stored_expires if stored_expires > 0 else 3600
    payload = {
        GOG_CLIENT_ID: {
            "access_token": store.access_token(),
            "refresh_token": store.refresh_token(),
            "expires_in": expires_in,
            "loginTime": fetched_at,
        }
    }
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return config_path


def write_auth_config_now(store: GogTokenStore, config_path: str) -> str:
    """Like :func:`write_auth_config` but resets ``loginTime`` to the current
    time, so the freshly-refreshed token is treated as brand-new by gogdl."""
    payload = {
        GOG_CLIENT_ID: {
            "access_token": store.access_token(),
            "refresh_token": store.refresh_token(),
            "expires_in": store.expires_in() or 3600,
            "loginTime": int(time.time()),
        }
    }
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return config_path


def download_command(
    game_id: str,
    install_path: str,
    auth_config: str,
    *,
    platform: str = "windows",
    lang: str | None = None,
    skip_dlcs: bool = True,
    max_workers: int = 4,
) -> list[str]:
    """Build the gogdl ``download`` command for ``game_id``.

    Downloads the game depot into ``install_path``. If ``lang`` is omitted the
    system locale is used; ``skip_dlcs`` avoids pulling every owned DLC. A modest
    ``max_workers`` prevents gogdl from exhausting connections/inotify and
    stalling on large manifests (a known gogdl issue on many-core machines).
    """
    cmd = [
        gogdl_binary(),
        "--auth-config-path",
        auth_config,
        "download",
        str(game_id),
        "--path",
        install_path,
        "--platform",
        platform,
        "--max-workers",
        str(max_workers),
    ]
    if lang:
        cmd += ["--lang", lang]
    if skip_dlcs:
        cmd.append("--skip-dlcs")
    return cmd


def manifest_path(game_id: str) -> str:
    """Return gogdl's global manifest path for a product."""
    config_root = os.environ.get(GOGDL_CONFIG_ENV)
    if not config_root:
        config_root = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "heroic_gogdl",
        )
    return os.path.join(config_root, "manifests", str(game_id))


def manifest_data(game_id: str) -> dict:
    """Read gogdl's persisted product manifest, if one exists."""
    try:
        with open(manifest_path(game_id), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def has_manifest(game_id: str) -> bool:
    return bool(manifest_data(game_id))


def repair_command(
    game_id: str,
    install_path: str,
    auth_config: str,
    *,
    platform: str = "windows",
    skip_dlcs: bool = True,
    max_workers: int = 4,
) -> list[str]:
    """Build gogdl's verification/repair command for an existing manifest."""
    cmd = [
        gogdl_binary(),
        "--auth-config-path",
        auth_config,
        "repair",
        str(game_id),
        "--path",
        install_path,
        "--platform",
        platform,
        "--max-workers",
        str(max_workers),
    ]
    if skip_dlcs:
        cmd.append("--skip-dlcs")
    return cmd


def import_info(game_id: str, install_path: str, auth_config: str) -> dict:
    """Return ``gogdl import`` structured info for an installed game.

    ``import`` reads the ``goggame-*.info`` manifest and yields the install
    directory, executable, and language. Falls back to ``{}`` on failure so
    callers can continue with best-effort detection.
    """
    import subprocess

    cmd = [
        gogdl_binary(),
        "--auth-config-path",
        auth_config,
        "import",
        install_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired, GogdlError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def executable_from_info(info: dict, install_path: str) -> str | None:
    """Pick the game executable from ``gogdl import`` info.

    Returns an absolute host filesystem path to the game's main executable so
    Vitrine can hand it straight to Wine (which auto-mounts it via ``Z:``).
    gogdl reports its primary ``FileTask`` under ``tasks`` (e.g.
    ``HuniePop.exe`` relative to the install root); we join it to the game dir
    and return the real path.
    """
    exe = None
    for task in info.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if task.get("category") == "game" and task.get("path"):
            exe = task["path"]
            break
    if not exe:
        exe = info.get("executable") or info.get("game_executable") or info.get("exe")
    if not exe or not isinstance(exe, str):
        return None
    clean = exe.replace("\\\\", "/").replace("\\", "/")
    if os.path.isabs(clean):
        # A Windows-style C:/... path: strip the drive and rebase onto install.
        cleaned = clean.split(":", 1)[-1].lstrip("/")
        candidate = os.path.join(install_path, cleaned)
    else:
        candidate = os.path.join(install_path, clean)
    candidate = os.path.realpath(candidate)
    return candidate if os.path.isfile(candidate) else None


def install_is_valid(game_id: str, install_path: str) -> bool:
    """True when ``install_path`` holds a valid goggame-*.info for ``game_id``.

    gogdl writes the depot into ``<install_path>/<InstallDirectory>/`` (it
    appends the game's install-dir name from the manifest), so the ``goggame``
    marker may sit one level down.
    """
    return find_game_dir(game_id, install_path) is not None


def find_game_dir(game_id: str, install_path: str) -> str | None:
    """Locate the directory holding ``goggame-<game_id>.info`` under a depot
    root. Returns ``None`` if no matching install marker is found."""

    if not os.path.isdir(install_path):
        return None
    marker = f"goggame-{game_id}.info"
    if os.path.isfile(os.path.join(install_path, marker)):
        return install_path
    for entry in sorted(os.listdir(install_path)):
        sub = os.path.join(install_path, entry)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, marker)):
            return sub
    install_directory = manifest_data(game_id).get("installDirectory")
    if install_directory:
        candidate = os.path.join(install_path, str(install_directory))
        if os.path.isdir(candidate):
            return candidate
    return None


def find_executable(install_path: str) -> str | None:
    """Find a likely game executable in a gogdl depot install."""
    if not os.path.isdir(install_path):
        return None
    candidates: list[str] = []
    for root, _dirs, files in os.walk(install_path):
        for name in files:
            if not name.lower().endswith(".exe"):
                continue
            lowered = name.casefold()
            if any(skip in lowered for skip in ("setup", "install", "unins", "redist", "dxsetup")):
                continue
            candidates.append(os.path.join(root, name))
    return sorted(candidates, key=lambda path: ("launcher" in os.path.basename(path).casefold(), path))[0] if candidates else None


def install_dir(slug: str) -> str:
    """Default depot install directory for a GOG game, under Vitrine's data dir."""
    from ... import paths

    return str(paths.data_dir() / "gog" / (slug or "game"))
