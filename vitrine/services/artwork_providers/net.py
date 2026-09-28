"""Low-level HTTP helpers shared by all artwork providers.

Centralises the requests calls, User-Agent and the shared rate limiter so every
provider throttles together and the engine stays GUI-free. Providers never throw
on network errors here; they return ``None``/empty so the orchestration layer
can fall through to the next provider.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "Vitrine/0.1 (game library launcher; artwork metadata lookup)"

#: How many HTTP requests may be sent before throttling kicks in.
_RATE_LIMIT_EVERY = 512
#: Additional seconds of sleep added for each throttled batch (1st, 2nd, ...).
_RATE_LIMIT_STEP = 10.0


class _RateLimiter:
    """Throttle an external API/art host during large back-fills.

    Tracks request volume and, after every ``_RATE_LIMIT_EVERY`` requests,
    pauses for a duration that grows with each batch -- so a 700-game back-fill
    does not trip the host's rate limiter while still recovering quickly on
    small runs. ``acquire()`` is thread-safe (multiple art workers share it).
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._count = 0
        self._batches = 0

    def acquire(self) -> None:
        import time

        with self._lock:
            self._count += 1
            if self._count >= _RATE_LIMIT_EVERY:
                self._count = 0
                self._batches += 1
                sleep_for = _RATE_LIMIT_STEP * self._batches
            else:
                return
        # Sleep outside the lock so other workers keep making progress.
        logger.info("Artwork rate-limit pause: %.0fs (batch %d)", sleep_for, self._batches)
        time.sleep(sleep_for)


_RATE_LIMITER = _RateLimiter()


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if extra:
        headers.update(extra)
    return headers


def get_bytes(url: str, timeout: int = 20, **kwargs: Any) -> bytes | None:
    """GET ``url`` and return raw bytes, or ``None`` on any failure."""
    _RATE_LIMITER.acquire()
    try:
        response = requests.get(url, headers=_headers(kwargs.get("headers")), timeout=timeout)
        response.raise_for_status()
        return response.content
    except (requests.RequestException, ValueError):  # noqa: BLE001
        logger.debug("Artwork HTTP GET failed %s", url)
        return None


def get_json(url: str, timeout: int = 20, headers: dict[str, str] | None = None) -> Any:
    """GET ``url`` and parse JSON, or ``None`` on failure."""
    data = get_bytes(url, timeout=timeout, headers=headers)
    if data is None:
        return None
    import json

    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):  # not JSON
        logger.debug("Artwork HTTP response not JSON %s", url)
        return None


def post_json(
    url: str, *, headers: dict[str, str] | None = None, data: Any = None, timeout: int = 20
) -> Any:
    """POST ``url`` (JSON body or raw string ``data``) and parse JSON on success."""
    _RATE_LIMITER.acquire()
    try:
        response = requests.post(
            url,
            headers=_headers(headers),
            data=data,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):  # noqa: BLE001
        logger.debug("Artwork HTTP POST failed %s", url)
        return None