"""Thin wrapper around the ``legendary`` CLI.

Legendary is the storeless Epic Games client Vitrine uses as its backend: it
authenticates, lists owned and installed games, installs, and launches with the
account's online session. We shell out to it rather than reimplementing Epic's
download paths.

All commands run with a timeout and raise :class:`LegendaryError` on failure so
callers never block or spin the GUI.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Env override for the legendary binary (used by tests and unusual setups).
LEGENDARY_ENV = "VITRINE_LEGENDARY"

#: Default install base legendary uses (also where it stores its own DB).
DEFAULT_INSTALL_DIR = os.path.expanduser("~/Games")

#: Path to legendary's per-user configuration/credential directory.
LEGENDARY_CONFIG = ("~/.config/legendary", "~/.config/legendary-gl")

#: Timeout for list metadata queries (can be slow on first run).
LIST_TIMEOUT = 60
#: Timeout for auth/status (fast).
AUTH_TIMEOUT = 30


class LegendaryError(Exception):
    """Raised when a legendary invocation fails or the binary is missing."""


def legendary_binary() -> str:
    """Return the legendary executable path, or raise if not installed."""
    override = os.environ.get(LEGENDARY_ENV)
    if override:
        return override
    path = shutil.which("legendary")
    if not path:
        raise LegendaryError(
            "Legendary is not installed. Install it (e.g. 'pip install legendary-gl' "
            "or your distro's package) and put 'legendary' on PATH."
        )
    return path


def is_installed() -> bool:
    try:
        legendary_binary()
        return True
    except LegendaryError:
        return False


def _run(args: Sequence[str], timeout: int = LIST_TIMEOUT, env: dict | None = None) -> subprocess.CompletedProcess:
    binary = legendary_binary()
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        return subprocess.run(
            [binary, *map(str, args)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
        )
    except FileNotFoundError as exc:
        raise LegendaryError(f"Legendary not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LegendaryError(f"Legendary timed out: {shlex.join(map(str, args))}") from exc


def _require_success(result: subprocess.CompletedProcess, action: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LegendaryError(f"legendary {action} failed: {detail}")


def auth(code: str) -> None:
    """Import an OAuth **exchange** code into legendary's credential store.

    Legendary's ``auth`` command exposes two flags: ``--code`` (authorization
    code) and ``--token`` (exchange token). Epic's web login delivers an
    *exchange* code, so use ``--token`` -- that is the battle-tested path
    legendary itself uses after a webview login.
    """
    _require_success(_run(["auth", "--token", code], timeout=AUTH_TIMEOUT), "auth")


def list_games() -> list[dict]:
    """Return every owned, installable game as structured metadata.

    ``legendary list --json`` returns ``{"game": [...], "dlc": [...]}``; each
    game entry carries ``app_name``, ``title``, ``slug``, ``installed`` and
    ``base_url``/``image`` used for artwork lookup.
    """
    result = _run(["list", "--json", "--include-ue"])
    _require_success(result, "list")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise LegendaryError("legendary list returned no JSON") from exc
    games = payload.get("game") if isinstance(payload, dict) else payload
    return games if isinstance(games, list) else []


def list_installed() -> list[dict]:
    result = _run(["list-installed", "--json", "--show-dirs"])
    if result.returncode != 0:
        return []  # not installed / not logged in yet is not fatal
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    return payload if isinstance(payload, list) else []


def dry_run_launch(app_name: str) -> list[str]:
    """Return the command line legendary would use to launch ``app_name``.

    Runs ``legendary launch <app> --dry-run`` which prints a shell line. This
    lets Vitrine supervise the actual process (for playtime tracking) instead
    of spawning an untracked legendary child.
    """
    result = _run(["launch", app_name, "--dry-run"], timeout=AUTH_TIMEOUT)
    _require_success(result, f"launch --dry-run {app_name}")
    line = (result.stdout or result.stderr or "").strip()
    return shlex.split(line)


def install(app_name: str, base_path: str | None = None, *, skip_dlcs: bool = True) -> None:
    """Install ``app_name`` via legendary. Blocks until the download finishes."""
    args: list[str] = ["install", app_name]
    if base_path:
        args += ["--base-path", base_path]
    if skip_dlcs:
        args.append("--skip-dlcs")
    _require_success(_run(args, timeout=LIST_TIMEOUT), "install")


def launch(app_name: str, *, no_wine: bool = False) -> None:
    """Launch ``app_name`` through legendary in a new process.

    Launched detached so legendary's process (which supervises the game under
    wine) is not tied to Vitrine's lifetime; playtime is tracked via the
    ``Runtime`` only for native commands produced by ``dry_run_launch``.
    """
    args: list[str] = ["launch", app_name]
    if no_wine:
        args.append("--no-wine")
    binary = legendary_binary()
    try:
        subprocess.Popen([binary, *args], env=dict(os.environ), start_new_session=True)
    except FileNotFoundError as exc:
        raise LegendaryError(f"Legendary not found: {binary}") from exc