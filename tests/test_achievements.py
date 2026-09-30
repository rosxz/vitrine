"""Achievements: orchestrator, provider selection, Steam provider parsing."""

from __future__ import annotations

from vitrine.domain.achievement import Achievement, AchievementSet
from vitrine.services import achievements as svc
from vitrine.services.achievement_providers import steam as steam_mod


class _FakeGame:
    def __init__(
        self,
        *,
        source: str = "steam",
        source_id: str | None = "123",
        name: str = "Game",
        achievements_source: str = "auto",
        id: int | None = 1,
    ) -> None:
        self.source = source
        self.source_id = source_id
        self.name = name
        self.achievements_source = achievements_source
        self.id = id


def test_provider_for_follows_store_source() -> None:
    assert svc.provider_for(_FakeGame(source="steam")) == "steam"
    assert svc.provider_for(_FakeGame(source="gog")) == "gog"
    assert svc.provider_for(_FakeGame(source="epic")) == "epic"
    # Local games have no store provider.
    assert svc.provider_for(_FakeGame(source="local")) is None
    # Explicit off overrides auto.
    assert svc.provider_for(_FakeGame(source="steam", achievements_source="none")) is None


def test_provider_for_ra_only_postponed() -> None:
    # RA is designed-in but not implemented yet: the provider id resolves but the
    # provider is not registered, so fetching yields None (no crash).
    game = _FakeGame(source="local", achievements_source="retroachievements")
    assert svc.provider_for(game) == "retroachievements"
    from vitrine.services.achievement_providers import base as providers

    assert providers.has_provider("retroachievements") is False


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")


def test_steam_provider_parses_schema_and_progress(monkeypatch) -> None:
    schema = {
        "game": {
            "availableGameStats": {
                "achievements": [
                    {
                        "name": "KILL_1",
                        "displayName": "First",
                        "description": "Kill one",
                        "hidden": False,
                        "icon": "http://i/KILL_1.png",
                        "icongray": "http://i/KILL_1_g.png",
                    },
                    {
                        "name": "KILL_10",
                        "displayName": "Ten",
                        "description": "Kill ten",
                        "hidden": True,
                        "icon": "http://i/KILL_10.png",
                        "icongray": "http://i/KILL_10_g.png",
                    },
                ]
            }
        }
    }
    progress = {
        "playerstats": {"achievements": [{"apiname": "KILL_1", "achieved": 1, "unlocktime": 1700000000}]}
    }
    monkeypatch.setattr(steam_mod, "_get_schema", lambda *a: schema)
    monkeypatch.setattr(steam_mod, "_get_player_achievements", lambda *a: progress)

    ctx = {"steam_api_key": "KEY", "steamid64": "7656111"}
    result = steam_mod.fetch(_FakeGame(source="steam", source_id="440"), ctx)
    assert result is not None
    assert result.total == 2
    assert result.unlocked == 1
    by_key = {a.key: a for a in result.achievements}
    assert by_key["KILL_1"].unlocked is True
    assert by_key["KILL_1"].unlock_date == 1700000000
    assert by_key["KILL_1"].icon_unlocked_url == "http://i/KILL_1.png"
    assert by_key["KILL_10"].hidden is True
    assert by_key["KILL_10"].unlocked is False


def test_steam_provider_ignores_missing_key() -> None:
    result = steam_mod.fetch(_FakeGame(source="steam", source_id="440"), {"steam_api_key": "", "steamid64": ""})
    assert result is None


def test_steam_provider_private_profile_returns_definitions(monkeypatch) -> None:
    schema = {
        "game": {
            "availableGameStats": {
                "achievements": [
                    {"name": "A", "displayName": "A", "description": "", "hidden": False}
                ]
            }
        }
    }
    monkeypatch.setattr(steam_mod, "_get_schema", lambda *a: schema)
    monkeypatch.setattr(steam_mod, "_get_player_achievements", lambda *a: None)
    result = steam_mod.fetch(_FakeGame(source="steam", source_id="440"), {"steam_api_key": "K", "steamid64": "ID"})
    assert result is not None
    assert result.total == 1
    assert result.achievements[0].unlocked is False


def test_newly_unlocked_detects_fresh_achievements() -> None:
    old = AchievementSet.build("steam", [Achievement(key="a", unlocked=False), Achievement(key="b", unlocked=True)])
    new = AchievementSet.build("steam", [Achievement(key="a", unlocked=True), Achievement(key="b", unlocked=True)])
    fresh = svc.newly_unlocked(old, new)
    assert [a.key for a in fresh] == ["a"]


def test_configured_dispatch(monkeypatch) -> None:
    """fetch_achievements routes to the right provider and returns None when off."""
    monkeypatch.setattr(
        steam_mod,
        "_get_schema",
        lambda *a: {"game": {"availableGameStats": {"achievements": [{"name": "A", "displayName": "A"}]}}},
    )
    monkeypatch.setattr(steam_mod, "_get_player_achievements", lambda *a: None)
    ctx = {
        "steam_api_key": "K",
        "steamid64": "ID",
        "gog_access_token": None,
        "gog_user_id": None,
        "epic_available": True,
    }
    result = svc.fetch_achievements(_FakeGame(source="steam"), ctx)
    assert result is not None
    assert result.provider == "steam"
    # A local game with no provider -> None.
    assert svc.fetch_achievements(_FakeGame(source="local"), ctx) is None

def test_normalise_icons_to_uniform_square(monkeypatch, tmp_path) -> None:
    """Cached achievement icons must be one fixed size regardless of the source
    image's dimensions/aspect, so the viewer shows them all consistently."""
    from io import BytesIO

    from PIL import Image

    from vitrine.services import achievements as svc

    # A portrait and a landscape source image, both very different sizes.
    portrait = Image.new("RGB", (200, 400))
    landscape = Image.new("RGB", (500, 120))

    def _png_bytes(img: Image.Image) -> bytes:
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    for src in (portrait, landscape):
        normalised = svc._normalise(_png_bytes(src))
        img = Image.open(BytesIO(normalised))
        assert img.size == (svc.ICON_CACHE_SIZE, svc.ICON_CACHE_SIZE), img.size


def test_centre_square_crops() -> None:
    from PIL import Image

    from vitrine.services import achievements as svc

    img = Image.new("RGB", (300, 100))
    cropped = svc._centre_square(img)
    assert cropped.size == (100, 100)
