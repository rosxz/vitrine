"""Installable Wine/Proton runners fetched from Lutris' catalog.

Mirrors Lutris' runner manager: ``vitrine/runners_source`` queries the Lutris
runners API for the "wine" runner's published build versions, and downloads the
user's chosen one into Vitrine's runner directory (``runners_dir()``). Downloaded
archives unpack to a folder containing a ``bin/wine`` (or similar) binary, which
the runner store then references.
"""

from __future__ import annotations

import json
import logging
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import paths

logger = logging.getLogger(__name__)

#: Lutris runners catalog (public API, no auth).
LUTRIS_RUNNERS_URL = "https://lutris.net/api/runners"

#: Timeout for metadata + download requests.
TIMEOUT = 60


def available_runners() -> list[dict]:
    """Return the Wine builds Lutris offers, newest-first.

    Each entry: ``{id, name, version, url}`` where ``id`` is a stable slug we
    register in the runner store.
    """
    try:
        import urllib.request as u

        with u.urlopen(LUTRIS_RUNNERS_URL, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - offline/catalog down should not crash
        logger.warning("Could not fetch Lutris runners: %s", exc)
        return []

    for runner in payload.get("results") or []:
        if runner.get("slug") != "wine":
            continue
        versions = runner.get("versions") or []
        out = []
        for v in versions:
            if not v.get("url"):
                continue
            version = v.get("version") or "latest"
            name = f"Wine {version}"
            slug = _slug_from(name)
            out.append({"id": slug, "name": name, "version": version, "url": v["url"]})
        # Most useful first (defaults first, then reverse chronological).
        out.sort(key=lambda e: (e.get("version") or "") == "wine" or 0, reverse=True)
        return out
    return []


def _slug_from(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "wine-runner"


def download_runner(entry: dict) -> str:
    """Download and unpack ``entry`` into the runner directory.

    Returns the absolute path to the unpacked runner directory (which contains
    a wine binary). Raises on failure.
    """
    url = entry["url"]
    dest_dir = paths.runners_dir() / entry["id"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Download to a temp file first so a partial download never looks complete.
    with tempfile.NamedTemporaryFile(suffix=Path(url).suffix or ".tgz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        logger.info("Downloading %s from %s", entry["id"], url)
        urllib.request.urlretrieve(url, tmp_path)  # noqa: S310 - user-triggered download

    try:
        if tmp_path.suffix.lower() in (".zip",):
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(dest_dir)
        else:
            with tarfile.open(tmp_path) as tf:
                _safe_extract(tf, dest_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    # If the archive wrapped everything in one subfolder, flatten it up one level
    # so the runner dir directly contains bin/wine.
    _flatten(dest_dir)
    return str(dest_dir)


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise OSError(f"Unsafe tar member: {member.name}")
    tf.extractall(dest)


def _flatten(dest: Path) -> None:
    """Move a lone nested directory's contents up into ``dest``."""
    children = [p for p in dest.iterdir() if p.is_dir()]
    if len(children) != 1 or any(p.is_file() for p in dest.iterdir()):
        return
    nested = children[0]
    for item in list(nested.iterdir()):
        item.rename(dest / item.name)
    nested.rmdir()