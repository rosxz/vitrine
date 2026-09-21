"""Tests for the GOG source: durable auth store and owned-library sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from vitrine.sources.gog.auth import GogAuthError, GogCookieJar, GogTokenStore
from vitrine.sources.gog_source import USER_SETTING, GogSource

# -- token store ---------------------------------------------------------------


def _store(tmp_path: Path, user_id: str = "48628349971017") -> GogTokenStore:
    return GogTokenStore(tmp_path, user_id)


def test_token_store_round_trip_and_clear(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not store.exists()
    store.set_credentials(GogCookieJar([{"name": "gog_lci", "value": "abc"}]), access_token="tok-1")
    assert store.exists()
    assert store.access_token() == "tok-1"
    assert store.is_authenticated() is True

    # clear() removes the credential file.
    store.clear()
    assert not store.exists()
    assert store.is_authenticated() is False


def test_token_store_preserves_session_cookies(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jar = GogCookieJar(
        [
            {"name": "gog_lci", "value": "tok"},
            {"name": "gog_us", "value": "user", "expires": None},  # session cookie, no expiry
        ]
    )
    store.set_credentials(jar, access_token="tok")
    reloaded = store.cookies()
    assert reloaded.get("gog_us") == "user"
    assert reloaded.get("gog_lci") == "tok"
    assert store.access_token() == "tok"


def test_unauthenticated_store_reports_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.is_authenticated() is False


# -- sync ----------------------------------------------------------------------

@pytest.fixture
def library(tmp_path: Path) -> object:
    from vitrine import db
    from vitrine.library import Library

    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def _seed_token(tmp_path: Path, user_id: str = "48628349971017") -> None:
    _store(tmp_path, user_id).set_credentials(GogCookieJar([]), access_token="tok-123")


def _owned_payload() -> dict:
    return {
        "totalPages": 1,
        "products": [
            {
                "id": 1207658691,
                "title": "Shadowrun Returns",
                "slug": "shadowrun_returns",
                "worksOn": {"Windows": True, "Mac": True, "Linux": True},
            },
            {
                "id": 1207664663,
                "title": "Witcher 3: Wild Hunt, The ",
                "slug": "the_witcher_3_wild_hunt",
            },
        ],
    }


def test_sync_requires_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object) -> None:
    from vitrine.sources import gog_source as gog_mod

    monkeypatch.setattr(gog_mod.paths, "secret_dir", lambda: tmp_path)
    src = GogSource(library)  # type: ignore[call-arg]
    with pytest.raises(GogAuthError):
        src.sync()


def test_sync_with_auth_populates_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    from vitrine.sources import gog_source as gog_mod

    _seed_token(tmp_path)
    monkeypatch.setattr(gog_mod.paths, "secret_dir", lambda: tmp_path)
    monkeypatch.setattr(library, "set_setting", lambda _k, _v: None)
    monkeypatch.setattr(gog_mod, "_get_json", lambda _url, token, params=None: _owned_payload())

    src = GogSource(library)  # type: ignore[call-arg]
    src.user_id = "48628349971017"
    count = src.sync()
    assert count == 2

    names = {r["name"] for r in library.source_games("gog")}
    assert "Shadowrun Returns" in names

    grid = {g.name for g in library.games(source="gog")}
    assert "Shadowrun Returns" in grid
    assert "Witcher 3: Wild Hunt, The " in grid


def test_sync_promotes_lutris_artwork_and_paginates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    from vitrine.sources import gog_source as gog_mod

    _seed_token(tmp_path)
    monkeypatch.setattr(gog_mod.paths, "secret_dir", lambda: tmp_path)
    monkeypatch.setattr(library, "set_setting", lambda _k, _v: None)

    page = {"a": 0}

    def _fake_json(url, token, params=None):  # noqa: ARG001
        page_no = params and int(params.get("page", 1))
        if page_no <= 1:
            page["a"] = 2
            return {"totalPages": 2, "products": [{"id": 1, "title": "Alpha"}]}
        if page_no == 2:
            return {"totalPages": 2, "products": [{"id": 2, "title": "Beta"}]}
        return {"totalPages": 2, "products": []}

    monkeypatch.setattr(gog_mod, "_get_json", _fake_json)

    src = GogSource(library)  # type: ignore[call-arg]
    src.user_id = "48628349971017"
    count = src.sync()
    assert count == 2

    alpha = library.game_by_source_id("gog", "1")
    assert alpha is not None
    assert alpha.artwork_source == "lutris"


# -- login helpers -------------------------------------------------------------

def test_extract_code_from_redirect() -> None:
    from vitrine.ui.gog_login_dialog import _extract_code

    url = "https://embed.gog.com/on_login_success?origin=client&code=abc123"
    assert _extract_code(url) == "abc123"


def test_extract_code_missing() -> None:
    from vitrine.ui.gog_login_dialog import _extract_code

    assert _extract_code("https://embed.gog.com/on_login_success?origin=client") == ""


def test_user_setting_key() -> None:
    assert USER_SETTING == "gog_user_id"


def test_exchange_code_uses_post_form_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOG's /token endpoint must be hit with POST + form data (GET gives 400)."""
    from vitrine.sources.gog import auth as gog_auth

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"access_token": "tok-abc", "user_id": "48628349971017"}

    def _fake_post(url, data=None, timeout=None):  # noqa: ARG001
        captured["url"] = url
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(gog_auth.requests, "post", _fake_post)
    result = gog_auth.exchange_code_for_token("mycode")

    assert result["access_token"] == "tok-abc"
    assert captured["url"] == gog_auth.TOKEN_URL
    assert captured["data"]["code"] == "mycode"
    assert captured["data"]["grant_type"] == "authorization_code"
    # redirect_uri must match exactly what was sent at the start of the flow.
    assert captured["data"]["redirect_uri"] == gog_auth.AUTH_REDIRECT_URI


def test_exchange_code_requires_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import auth as gog_auth

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {}

    monkeypatch.setattr(gog_auth.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(GogAuthError):
        gog_auth.exchange_code_for_token("mycode")

def test_offline_installer_uses_windows_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import installer as gog_installer

    store = _store(tmp_path)
    store.set_credentials(GogCookieJar([]), access_token="tok-gog")

    gamedata = {
        "data": {
            "1207658691": {
                "downloads": {
                    "windows": [
                        {"manualUrl": "/downlink/ut2k4/en1installer1",
                         "type": "installer", "size": 100},
                        {"manualUrl": "/downlink/ut2k4/en1installer2",
                         "type": "installer", "size": 200},
                        {"manualUrl": "/downlink/ut2k4/bonus", "type": "bonus", "size": 5},
                    ]
                }
            }
        }
    }

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return gamedata

    monkeypatch.setattr(gog_installer.requests, "post", lambda *a, **k: _Resp())
    url = gog_installer.offline_installer(store, "1207658691", "UT2004")
    assert "/downlink/ut2k4/en1installer2" in url
    assert "token=tok-gog" in url


def test_offline_installer_requires_auth(tmp_path: Path) -> None:
    from vitrine.sources.gog import installer as gog_installer

    store = _store(tmp_path)  # no credentials
    with pytest.raises(GogAuthError):
        gog_installer.offline_installer(store, "1207658691", "Game")
