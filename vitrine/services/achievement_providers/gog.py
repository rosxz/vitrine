"""GOG achievements via the Galaxy gameplay API.

``GET https://gameplay.gog.com/clients/{product_id}/users/{user_id}/achievements``
(Authorization: Bearer <access_token>) returns both the achievement definitions
(name, description, locked/unlocked icon URLs, visibility) and, on each item, a
``date_unlocked`` when this user has unlocked it -- so one call gives us the full
picture (including completion detection).
"""

from __future__ import annotations

from typing import Any

import requests

from vitrine.domain.achievement import Achievement, AchievementSet

ID = "gog"

#: Context keys (populated by services.achievements.load_context).
ACCESS_TOKEN_CTX = "gog_access_token"
USER_ID_CTX = "gog_user_id"

ENDPOINT = "https://gameplay.gog.com"
TIMEOUT = 20

#: An achievement whose accent is a comma-separated list; if only integers, this
#: is metadata, not a proper achievements flag. (Left for clarity; GOG returns
#: achievement keys independently per item.)


def configured(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get(ACCESS_TOKEN_CTX) and ctx.get(USER_ID_CTX))


def _fetch_raw(product_id: str, user_id: str, access_token: str) -> list[dict[str, Any]]:
    url = f"{ENDPOINT}/clients/{product_id}/users/{user_id}/achievements"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json().get("items") or []
    except (requests.RequestException, ValueError):  # noqa: BLE001
        return []


def fetch(game: Any, ctx: dict[str, Any]) -> AchievementSet | None:
    product_id = str(getattr(game, "source_id", "") or "")
    user_id = str(ctx.get(USER_ID_CTX) or "")
    access_token = str(ctx.get(ACCESS_TOKEN_CTX) or "")
    if not product_id or not configured(ctx):
        return None

    items = _fetch_raw(product_id, user_id, access_token)
    if not items:
        return None

    achievements: list[Achievement] = []
    for idx, item in enumerate(items):
        date_unlocked = item.get("date_unlocked")
        unlocked = bool(date_unlocked)
        achievements.append(
            Achievement(
                key=str(item.get("achievement_key") or item.get("achievement_id") or f"ach-{idx}"),
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                hidden=not bool(item.get("visible", True)),
                unlocked=unlocked,
                unlock_date=_to_unix(date_unlocked),
                progress=1.0 if unlocked else 0.0,
                icon_locked_url=item.get("image_url_locked"),
                icon_unlocked_url=item.get("image_url_unlocked"),
                sort=idx,
            )
        )
    return AchievementSet.build(provider=ID, items=achievements)


def _to_unix(value: Any) -> int | None:
    """GOG ``date_unlocked`` is an ISO string like ``2016-03-09T10:16:11+0000``."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    # ISO 8601 with offset; use datetime so we don't depend on pandas/dateutil.
    from datetime import datetime

    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None