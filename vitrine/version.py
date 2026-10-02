"""Runtime version and repository metadata for the About dialog."""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Latest tagged release (kept in sync with packaging/nix metadata).
RELEASE_VERSION = "0.9.3"
#: When the current release was cut (ISO date).
RELEASE_DATE = "2026-10-02"
#: Source repository, so the About dialog can link to it.
REPO_URL = "https://github.com/rosxz/vitrine"

_ROOT = Path(__file__).resolve().parent.parent


def git_hash() -> str:
    """Best-effort short git commit hash of the running source tree.

    Falls back to an empty string when the tree isn't a git checkout (e.g. the
    flatpak build ships a source snapshot) or git isn't available.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def version() -> str:
    """Human-facing version string, preferring the module's own tag/version."""
    from vitrine import __version__

    # __version__ is the pyproject baseline; RELEASE_VERSION is what this tree
    # was released as. Prefer the explicit release so the About tab reports the
    # installed version (0.9.2) rather than the dev baseline (0.0.1).
    return RELEASE_VERSION if RELEASE_VERSION != __version__ else __version__
