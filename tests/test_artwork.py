"""Artwork orchestration: source selection, refresh, and persistence."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from vitrine import artwork, db
from vitrine.library import Game, Library


@pytest.fixture
def library(tmp_path) -> Library:
    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


@pytest.fixture(autouse=True)
def _covers_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artwork.paths, "covers_dir", lambda: str(tmp_path / "covers"))


def _image_bytes(width: int, height: int, color: tuple = (120, 80, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "JPEG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, content: bytes, json_value=None) -> None:
        self.content = content
        self._json = json_value

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._json


def test_provider_source_uses_steam_cdn() -> None:
    game = Game(name="X", source="steam", source_id="123", artwork_source="provider")
    cover, banner = artwork.artwork_for(game)
    assert isinstance(cover, list) and cover and "123" in cover[0]
    assert isinstance(banner, list) and banner and "123" in banner[0]


def test_local_source_returns_no_urls() -> None:
    game = Game(name="X", artwork_source="local")
    assert artwork.artwork_for(game) == ("", "")


def test_lutris_source_uses_pinned_slug(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = library.add(Game(name="X", artwork_source="lutris", lutris_slug="pinned-slug"))
    monkeypatch.setattr(
        artwork, "lutris_art", lambda slug: (f"cover-{slug}.jpg", f"banner-{slug}.jpg")
    )
    monkeypatch.setattr(artwork, "_get", lambda *a, **k: _FakeResponse(b"", None))
    cover, banner = artwork.artwork_for(game)
    assert cover == "cover-pinned-slug.jpg"
    assert banner == "banner-pinned-slug.jpg"


def test_refresh_local_is_noop(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    game = library.add(Game(name="X", artwork_source="local"))
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: True)
    assert artwork.refresh_game_artwork(library, game) is False


def test_refresh_provider_persists_and_is_idempotent(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = library.add(
        Game(name="X", source="steam", source_id="123", artwork_source="provider")
    )
    monkeypatch.setattr(artwork, "steam_art", lambda _appid: ("cov.png", "ban.png"))
    monkeypatch.setattr(
        artwork, "_get", lambda *a, **k: _FakeResponse(_image_bytes(600, 900))
    )

    assert artwork.refresh_game_artwork(library, game) is True
    reloaded = library.game(game.id)
    assert reloaded.cover.endswith("123--cover-.jpg")
    assert reloaded.banner.endswith("123--banner-.jpg")

    # A second (non-forced) refresh must find cached art and change nothing.
    before_cover = reloaded.cover
    assert artwork.refresh_game_artwork(library, game) is False
    assert library.game(game.id).cover == before_cover


def test_fetch_game_artwork_is_db_free(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async pipeline path fetches without a DB handle; persist is the caller's job."""
    game = library.add(
        Game(name="X", source="steam", source_id="55", artwork_source="provider")
    )
    monkeypatch.setattr(artwork, "steam_art", lambda _appid: ("cov.png", "ban.png"))
    monkeypatch.setattr(
        artwork, "_get", lambda *a, **k: _FakeResponse(_image_bytes(600, 900))
    )

    # capture the library's update to prove it is NOT called by the fetch.
    calls: list = []
    monkeypatch.setattr(library, "update", lambda game: calls.append(game))

    assert artwork.fetch_game_artwork(game) is True
    assert calls == [], "fetch_game_artwork must not persist; caller does"
    assert game.cover is not None  # still mutates the game object in place

    # Idempotent once cached (non-forced).
    assert artwork.fetch_game_artwork(game) is False


def test_force_refresh_redownloads(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = library.add(
        Game(name="X", source="steam", source_id="9", artwork_source="provider")
    )
    cover_url, banner_url = "cov.png", "ban.png"
    monkeypatch.setattr(artwork, "steam_art", lambda _appid: (cover_url, banner_url))

    def _first_get(_url, **_k):
        return _FakeResponse(_image_bytes(600, 900, (120, 80, 40)))

    monkeypatch.setattr(artwork, "_get", _first_get)
    artwork.refresh_game_artwork(library, game)

    # Force: refetch with a different image; cached file color must differ.
    def _second_get(_url, **_k):
        return _FakeResponse(_image_bytes(600, 900, (200, 20, 20)))

    monkeypatch.setattr(artwork, "_get", _second_get)
    assert artwork.refresh_game_artwork(library, game, force=True) is True

    from PIL import Image

    path = artwork._cache_path("9", "cover")
    pixel = Image.open(path).convert("RGB").getpixel((0, 0))
    assert all(abs(a - b) <= 4 for a, b in zip(pixel, (200, 20, 20), strict=True))


def test_refresh_with_no_urls_returns_false(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = library.add(Game(name="X", artwork_source="lutris", lutris_slug=None))
    monkeypatch.setattr(artwork, "pick_lutris_slug", lambda _name: "")
    assert artwork.refresh_game_artwork(library, game) is False


def test_rate_limiter_pauses_growing_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    limiter = artwork._RateLimiter()
    # Emulate < 512 requests: nothing happens.
    for _ in range(510):
        limiter.acquire()
    assert sleeps == []

    limiter.acquire()  # 512th request -> first batch, growing pause.
    limiter.acquire()  # 513th -> under the threshold, no pause.
    assert len(sleeps) == 1
    assert sleeps[0] == artwork._RATE_LIMIT_STEP * 1

    # Cross the threshold again: the pause grows by another step.
    for _ in range(artwork._RATE_LIMIT_EVERY):
        limiter.acquire()
    assert len(sleeps) == 2
    assert sleeps[1] == artwork._RATE_LIMIT_STEP * 2


def test_steam_art_returns_fallback_candidates() -> None:
    cover, banner = artwork.steam_art("123")
    # The portrait cover tries the hi-res capsule first, then falls back to the
    # header and small capsule so missing assets still produce a tile cover.
    assert "123" in cover[0]
    assert any("header" in url for url in cover)
    assert any("capsule" in url for url in cover)
    assert banner and "capsule" in banner[0]