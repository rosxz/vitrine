"""Small helpers shared across the app."""

from __future__ import annotations

import os
import re
import time
import unicodedata

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_INITIAL_WORDS = re.compile(r"[^\w]+")


def slugify(value: str) -> str:
    """Turn a display name into a stable, filesystem-safe slug."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _SLUG_STRIP.sub("-", ascii_only.lower()).strip("-")


def now() -> int:
    return int(time.time())


def expand(path: str | None) -> str | None:
    """Expand ``~`` and environment variables in a user-supplied path."""
    if not path:
        return None
    return os.path.expandvars(os.path.expanduser(path))


def initials(name: str, limit: int = 2) -> str:
    """Up to ``limit`` initials, used for placeholder cover art."""
    words = [w for w in _INITIAL_WORDS.split(name) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:limit].upper()
    return "".join(word[0] for word in words[:limit]).upper()


def human_playtime(seconds_or_hours: float) -> str:
    """Format a playtime in hours as ``12.5 h`` / ``42 min``."""
    if not seconds_or_hours:
        return ""
    hours = float(seconds_or_hours)
    if hours < 1:
        return f"{round(hours * 60)} min"
    return f"{hours:.1f} h"
