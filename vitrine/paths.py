"""Filesystem locations used by Vitrine.

Everything lives under standard XDG directories, never inside a Lutris install:

    $XDG_DATA_HOME/vitrine/    library.db, runners/, prefixes/
    $XDG_CACHE_HOME/vitrine/   covers/, tokens, logs
    $XDG_CONFIG_HOME/vitrine/  user-editable settings
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "vitrine"


def _xdg(env_var: str, fallback: str) -> Path:
    value = os.environ.get(env_var)
    base = Path(value) if value else Path.home() / fallback
    return base / APP_DIR_NAME


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def db_path() -> Path:
    return data_dir() / "library.db"


def runners_dir() -> Path:
    """Downloaded Wine/Proton builds."""
    return data_dir() / "runners"


def prefixes_dir() -> Path:
    """Default location for Wine prefixes created by Vitrine."""
    return data_dir() / "prefixes"


def covers_dir() -> Path:
    """Normalised cover images (always the same aspect ratio)."""
    return cache_dir() / "covers"


def secret_dir() -> Path:
    """Tokens and other credentials."""
    return cache_dir() / "secrets"


def log_path() -> Path:
    return cache_dir() / "vitrine.log"


def ensure_dirs() -> None:
    for path in (data_dir(), cache_dir(), config_dir(), runners_dir(), prefixes_dir(), covers_dir(), secret_dir()):
        path.mkdir(parents=True, exist_ok=True)
