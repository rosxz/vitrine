"""Epic achievements via legendary's ``achievements`` CLI.

``legendary achievements <app_name> --json [--hidden]`` returns a JSON object
with ``total_achievements``, per-category buckets (``completed``/``in_progress`` /
``uninitiated``/``hidden``), and summary counts (``user_unlocked``, ``user_xp``).
Each achievement carries its display name, description, progress, unlock date,
icon link, tier and rarity. Vitrine shells out to legendary (which owns the Epic
session) rather than re-implementing Epic's GraphQL.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from vitrine.domain.achievement import Achievement, AchievementSet

ID = "epic"

#: Environment override for the legendary binary (mirrors source adapter).
LEGENDARY_ENV = "VITRINE_LEGENDARY"

TIMEOUT = 90


def configured(ctx: dict[str, Any]) -> bool:
    # Epic uses legendary's own stored session; no extra credential in ctx.
    return bool(ctx.get("epic_available", True))


def _legendary_binary() -> str | None:
    override = os.environ.get(LEGENDARY_ENV)
    if override:
        return override
    import shutil

    return shutil.which("legendary")


def _fetch_raw(app_name: str, show_hidden: bool = True) -> dict[str, Any] | None:
    binary = _legendary_binary()
    if not binary:
        return None
    cmd = [binary, "achievements", app_name, "--json"]
    if show_hidden:
        cmd.append("--hidden")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def fetch(game: Any, ctx: dict[str, Any]) -> AchievementSet | None:
    app_name = str(getattr(game, "source_id", "") or "")
    if not app_name:
        return None
    data = _fetch_raw(app_name)
    if not data:
        return None

    achievements: list[Achievement] = []
    for sort, entry in enumerate(_all(data)):
        achievements.append(
            Achievement(
                key=str(entry.get("name") or f"ach-{sort}"),
                name=str(entry.get("display_name") or entry.get("name") or ""),
                description=str(entry.get("description") or ""),
                hidden=bool(entry.get("hidden")),
                unlocked=bool(entry.get("unlocked")),
                unlock_date=_to_unix(entry.get("unlock_date")),
                progress=_clamp(entry.get("progress")),
                xp=_int_or_none(entry.get("xp")),
                tier=_str_or_none(entry.get("tier")),
                rarity=_float_or_none(entry.get("rarity")),
                icon_locked_url=entry.get("icon_link"),
                icon_unlocked_url=entry.get("icon_link"),
                sort=sort,
            )
        )
    return AchievementSet.build(
        provider=ID,
        items=achievements,
    )


def _all(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten legendary's four buckets, preserving intent (unlocked first)."""
    out: list[dict[str, Any]] = []
    for bucket in ("completed", "in_progress", "uninitiated", "hidden"):
        for entry in data.get(bucket) or []:
            if isinstance(entry, dict):
                out.append(entry)
    return out


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None and value != "" else None


def _to_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import datetime

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None