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
#: Classic Proton builds (GE-Proton), canonical source used by Lutris/umu.
PROTON_RELEASES_URL = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases?per_page=20"

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

    wine: list[dict] = []
    for runner in payload.get("results") or []:
        if runner.get("slug") != "wine":
            continue
        versions = runner.get("versions") or []
        for v in versions:
            if not v.get("url"):
                continue
            version = v.get("version") or "latest"
            name = f"Wine {version}"
            wine.append({"id": _slug_from(name), "name": name, "version": version, "url": v["url"], "kind": "wine"})
    # Defaults first, then reverse chronological.
    wine.sort(key=lambda e: (e.get("version") or "") == "wine" or 0, reverse=True)
    return wine + _available_protons()


def _available_protons() -> list[dict]:
    """Return installable GE-Proton builds (x86_64), newest first."""
    try:
        import urllib.request as u

        with u.urlopen(PROTON_RELEASES_URL, timeout=TIMEOUT) as resp:
            releases = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - offline/GitHub down must not crash
        logger.warning("Could not fetch Proton releases: %s", exc)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for release in releases or []:
        tag = release.get("tag_name") or ""
        if not tag or tag in seen:
            continue
        url = ""
        for asset in release.get("assets") or []:
            name = asset.get("name") or ""
            if name.endswith(".tar.gz") and "x86_64" in name:
                url = asset.get("browser_download_url") or ""
                break
        if not url:
            continue
        seen.add(tag)
        out.append({"id": _slug_from(tag), "name": tag, "version": tag, "url": url, "kind": "proton"})
    return out


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
    kind = entry.get("kind", "wine")
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
    # so the runner dir directly contains bin/wine or a proton script.
    _flatten(dest_dir)

    # Report the executable discovery should record: the proton script for a
    # Proton build, or bin/wine for a Wine build.
    proton_script = dest_dir / "proton"
    if kind == "proton" and proton_script.is_file():
        return str(proton_script)
    for rel in ("bin/wine", "bin/wine64", "files/bin/wine", "tools/wine/wine64"):
        candidate = dest_dir / rel
        if candidate.is_file():
            return str(candidate)
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