"""Locating a local Steam installation and its configuration.

Reads Steam's own files without touching Steam's running client, so the source
can work even when Steam is closed. Two families of files are used:

- ``libraryfolders.vdf`` lists every library root; each contains a
  ``steamapps`` directory with ``appmanifest_<appid>.acf`` files.
- ``config.vdf`` (or ``loginusers.vdf``) reveals which account is active, for
  resolving the 64-bit SteamID used by the store/API endpoints.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .vdf import parse_vdf_file

#: Fallback locations of a Steam installation's main data directory.
STEAM_DATA_DIRS = (
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
    "~/snap/steam/common/.local/share/Steam",
)

#: Environment override (used by tests and unusual setups).
STEAM_ROOT_ENV = "VITRINE_STEAM_ROOT"

_APPID_RE = re.compile(r"appmanifest_(\d+)\.acf")


def find_steam_root() -> str:
    """Return the Steam data directory, or ``""`` if none is found."""
    if override := os.environ.get(STEAM_ROOT_ENV):
        if os.path.isdir(os.path.join(override, "steamapps")):
            return override
    for candidate in STEAM_DATA_DIRS:
        path = os.path.expanduser(candidate)
        if path and os.path.isdir(path) and os.path.isdir(os.path.join(path, "steamapps")):
            return path
    return ""


def library_folders(steam_root: str) -> list[str]:
    """Return every library root listed in ``libraryfolders.vdf``."""
    library = parse_vdf_file(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"))
    entries = library.get("libraryfolders") or {}
    folders: list[str] = []
    for key, value in entries.items():
        if not str(key).isdigit():
            continue
        if isinstance(value, dict) and value.get("path"):
            folders.append(str(value["path"]))
    return folders


def steamapps_dirs(steam_root: str) -> list[str]:
    """All ``steamapps`` directories, starting with the main one."""
    roots = {steam_root, *(library_folders(steam_root))}
    return [os.path.join(root, "steamapps") for root in sorted(roots)]


def _config_value(config: dict[str, Any], *path: str) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict):
            return None
        found = None
        for candidate, value in current.items():
            if candidate.lower() == key.lower():
                found = value
                break
        if found is None:
            return None
        current = found
    return current


def active_steamid64(steam_root: str) -> str:
    """Return the 64-bit SteamID of the most-recently-used account."""
    config_path = os.path.join(steam_root, "config", "config.vdf")
    if os.path.isfile(config_path):
        config = parse_vdf_file(config_path)
        software = _config_value(config, "InstallConfigStore", "Software", "Valve", "Steam")
        if isinstance(software, dict) and software.get("AutoLoginUser"):
            username = str(software["AutoLoginUser"])
            users = _config_value(config, "InstallConfigStore", "Software", "Valve", "Steam", "Accounts", username)
            if isinstance(users, dict):
                for key, value in users.items():
                    if str(key).isdigit() and isinstance(value, dict) and value.get("PersonaName") is not None:
                        return str(key)

    # Fall back to loginusers.vdf, preferring the account marked most recent.
    login_path = os.path.join(steam_root, "config", "loginusers.vdf")
    if not os.path.isfile(login_path):
        return ""
    login = parse_vdf_file(login_path)
    accounts = login.get("users") or {}
    best_id = ""
    best_recent = -1
    for steamid, account in accounts.items():
        if not isinstance(account, dict):
            continue
        recent = str(account.get("mostrecent") or "0")
        if recent == "1":
            return str(steamid)
        try:
            if int(recent) > best_recent:
                best_recent = int(recent)
                best_id = str(steamid)
        except ValueError:
            continue
    return best_id


def appmanifest_paths(steamapps: str) -> list[str]:
    """All ``appmanifest_<appid>.acf`` paths in a steamapps directory."""
    try:
        return sorted(
            os.path.join(steamapps, name)
            for name in os.listdir(steamapps)
            if _APPID_RE.match(name) and name.endswith(".acf")
        )
    except OSError:
        return []


def appid_from_manifest_path(path: str) -> str | None:
    match = _APPID_RE.search(os.path.basename(path))
    return match.group(1) if match else None