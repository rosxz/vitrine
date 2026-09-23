"""Artwork providers & orchestrators: priority chaining, credentials, hint."""

from __future__ import annotations

import pytest

from vitrine import artwork, db
from vitrine.artwork_providers import base
from vitrine.library import Game, Library


@pytest.fixture
def library(tmp_path) -> Library:
    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


@pytest.fixture(autouse=True)
def _covers_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artwork.paths, "covers_dir", lambda: str(tmp_path / "covers"))


def _game(name="X", **kw) -> Game:
    kw.setdefault("artwork_source", "auto")
    return Game(name=name, **kw)


def test_default_priority_lists() -> None:
    tile, hero = artwork.load_priority()
    assert tile == list(artwork.DEFAULT_TILE_PRIORITY)
    assert hero == list(artwork.DEFAULT_HERO_PRIORITY)
    # SteamGridDB is the preferred hero provider.
    assert hero[0] == "steamgriddb"


def test_clean_priority_keeps_known_providers(library: Library) -> None:
    library.set_setting(artwork.TILE_PRIORITY_SETTING, ["lutris", "unknown", "igdb"])
    tile, _hero = artwork.load_priority(library)
    assert tile == ["lutris", "igdb", "steamgriddb", "steam"]


def test_credentials_read_from_settings_and_env(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(artwork.ENV_IGDB_CLIENT_ID, raising=False)
    library.set_setting(artwork.IGDB_CLIENT_ID_SETTING, "cid")
    library.set_setting(artwork.IGDB_CLIENT_SECRET_SETTING, "csec")
    creds = artwork.load_credentials(library)
    assert creds["igdb_client_id"] == "cid"
    assert creds["igdb_client_secret"] == "csec"
    monkeypatch.setenv(artwork.ENV_IGDB_CLIENT_ID, "env-cid")
    assert artwork.load_credentials(library)["igdb_client_id"] == "env-cid"  # env wins


def test_auto_with_no_keys_marks_hint_and_returns_empty(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    game = _game(name="Portal", artwork_source="auto")
    monkeypatch.setattr(
        artwork.providers, "candidates_for", lambda _g, _c: base.CandidateSet()
    )
    cover, banner = artwork.artwork_for(game, library)
    assert cover == "" and banner == ""
    # The hint is a one-time banner and is stored so it doesn't repeat.
    assert bool(library.setting(artwork.ARTWORK_HINT_SETTING, False)) is True
    assert artwork.provider_hint_pending(library) is False


def test_priority_chain_picks_first_provider_for_each_slot(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    library.set_setting(artwork.STEAMGRIDDB_KEY_SETTING, "k")
    monkeypatch.setattr(
        artwork.providers, "configured_providers", lambda creds: ["steamgriddb", "lutris"]
    )

    class _Cset:
        host = None  # attribute placeholder
        by_provider = {
            "steamgriddb": {
                "tile": [base.Art("steamgriddb", "tile", "sgd-tile")]
            },
            "lutris": {
                "tile": [base.Art("lutris", "tile", "lut-tile")],
                "hero": [base.Art("lutris", "hero", "lut-hero")],
            },
        }

    def _candidates(_game, creds):
        return _Cset()

    monkeypatch.setattr(artwork.providers, "candidates_for", _candidates)
    game = _game(name="X")
    cover, banner = artwork.artwork_for(game, library)
    # Tile from steamgriddb (priority), hero from lutris (only hero source).
    assert cover == ["sgd-tile"]
    assert banner == ["lut-hero"]


def test_provider_candidates_gathers_across_configured(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    library.set_setting(artwork.STEAMGRIDDB_KEY_SETTING, "k")
    seen: dict = {}

    def _candidates(game, creds):
        seen["game"] = game
        seen["creds"] = creds
        return base.CandidateSet()

    monkeypatch.setattr(artwork.providers, "candidates_for", _candidates)
    game = _game(name="X", source_id="10", source="steam")
    artwork.provider_candidates(library, game)
    assert seen["game"] is game
    assert seen["creds"]["steamgriddb_key"] == "k"


def test_local_source_noop(library: Library) -> None:
    game = _game(artwork_source="local")
    assert artwork.artwork_for(game, library) == ("", "")


def test_first_slot_uses_priority_order_top_wins() -> None:
    """The priority list is top → bottom = highest → lowest; the first provider
    that has the slot is chosen (a missing earlier provider falls through)."""
    cset = base.CandidateSet()
    cset.add(base.Art("igdb", "tile", "http://igdb-tile"))
    cset.add(base.Art("steamgriddb", "tile", "http://sgd-tile"))
    cset.add(base.Art("steam", "hero", "http://steam-hero"))
    cset.add(base.Art("lutris", "hero", "http://lutris-hero"))

    # igdb is listed first and has a tile → chosen.
    got = artwork._first_slot(cset, ["igdb", "steamgriddb", "steam", "lutris"], "tile")
    assert got == ["http://igdb-tile"]

    # steamegriddb first (igdb has a tile too) → igdb ignored, sgd wins.
    got = artwork._first_slot(cset, ["steamgriddb", "igdb", "lutris"], "tile")
    assert got == ["http://sgd-tile"]

    # No tile providers → falls to "" (hero not used for tile).
    assert artwork._first_slot(cset, ["lutris", "steam"], "tile") == ""

    # Hero: steam listed first → steam wins over lutris.
    got = artwork._first_slot(cset, ["steam", "lutris"], "hero")
    assert got == ["http://steam-hero"]


def test_steam_provider_label_is_provider() -> None:
    assert base.provider_label("steam") == "Provider"
    assert base.provider_label("igdb") == "IGDB"


def test_migrate_legacy_lutris_default_to_auto(library: Library) -> None:
    legacy = library.add(Game(name="Old", source="gog", source_id="1", artwork_source="lutris"))
    explicit = library.add(Game(name="Auto", source="gog", source_id="2", artwork_source="auto"))
    local = library.add(Game(name="L", source="local", artwork_source="lutris"))
    changed = library.migrate_artwork_source_default()
    assert changed == 2  # legacy gog + local lutris → auto
    assert library.game(legacy.id).artwork_source == "auto"
    assert library.game(local.id).artwork_source == "auto"
    assert library.game(explicit.id).artwork_source == "auto"  # untouched
    # Idempotent: running again changes nothing.
    assert library.migrate_artwork_source_default() == 0


def test_force_refresh_setting_overrides_cached_check(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global "refresh all artwork" re-pulls even for games that already have art."""
    import io

    from PIL import Image as PILImage

    class _Resp:
        def __init__(self) -> None:
            buf = io.BytesIO()
            PILImage.new("RGB", (600, 900), (10, 20, 30)).save(buf, "JPEG")
            self.content = buf.getvalue()

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(artwork, "_get", lambda _url, **_k: _Resp())
    monkeypatch.setattr(artwork, "steam_art", lambda _a: ("cov.png", "ban.png"))
    game = library.add(Game(name="X", source="steam", source_id="9", artwork_source="provider"))
    assert artwork.refresh_game_artwork(library, game, force=True)
    before = library.game(game.id).cover
    assert before

    # Plain (non-forced) refresh is idempotent and leaves it untouched.
    assert artwork.refresh_game_artwork(library, game) is False
    # With the global override in ctx, it forces a re-fetch (same provider/path).
    ctx = artwork.load_context(library)
    ctx["force_refresh"] = True
    assert artwork.refresh_game_artwork(library, game, ctx=ctx) is True
    assert library.game(game.id).cover == before


def test_fetch_respects_dim_and_never_upscales(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom ``dims`` caps the cache dimension but never enlarges the source."""
    import io

    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (2000, 900), (90, 100, 110)).save(buffer, "JPEG")

    class _Resp:
        content = buffer.getvalue()

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(artwork, "_get", lambda _url, **_k: _Resp())
    game = Game(name="X", source="steam", source_id="9", artwork_source="auto")
    assert artwork.fetch((["http://t"], ["http://h"]), game, force=True, dims=(360, 720))
    cover = PILImage.open(artwork._cache_path("9", "cover"))
    # Source 2000x900 cropped to 2:3 → 600x900 → capped by 360 → 240x360.
    assert cover.width <= 360 and cover.height <= 360
    assert cover.width / cover.height <= artwork.COVER_RATIO + 1e-3
    banner = PILImage.open(artwork._cache_path("9", "banner"))
    # Banner cropped to 16:9 → 1600x900 → capped by 720 → 720x405.
    assert banner.width <= 720 and banner.height <= 720
    assert abs(banner.width / banner.height - artwork.BANNER_RATIO) <= 1e-3