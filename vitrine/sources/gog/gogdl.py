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

from .auth import GOG_CLIENT_ID, GogTokenStore

#: Environment override for the bundled gogdl, mirroring VITRINE_LEGENDARY.
GOGDL_ENV = "VITRINE_GOGDL"


class GogdlError(Exception):
    """Raised when gogdl is missing or fails."""


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
    """Write Vitrine's GOG token into gogdl's expected auth-config format."""
    payload = {
        GOG_CLIENT_ID: {
            "access_token": store.access_token(),
            "refresh_token": store.refresh_token(),
            "expires_in": store.expires_in() or 3600,
            "loginTime": store.fetched_at() or int(time.time()),
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
) -> list[str]:
    """Build the gogdl ``download`` command for ``game_id``.

    Downloads the game depot into ``install_path``. If ``lang`` is omitted the
    system locale is used; ``skip_dlcs`` avoids pulling every owned DLC.
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
    ]
    if lang:
        cmd += ["--lang", lang]
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
    gogdl reports an executable relative to the install root; we join it to the
    depot directory and return the real path.
    """
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
    """True when ``install_path`` holds a valid goggame-*.info for ``game_id``."""
    import glob

    if not os.path.isdir(install_path):
        return False
    marker = [os.path.basename(f) for f in glob.glob(os.path.join(install_path, "goggame-*.info"))]
    return any(f.startswith(f"goggame-{game_id}") for f in marker)


def install_dir(slug: str) -> str:
    """Default depot install directory for a GOG game, under Vitrine's data dir."""
    from ... import paths

    return str(paths.data_dir() / "gog" / (slug or "game"))