"""Tests for the GOG source: durable auth store and owned-library sync."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from vitrine.library import Game
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
    assert alpha.artwork_source == "auto"


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

def test_offline_installer_resolves_largest_windows_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vitrine.sources.gog import installer as gog_installer

    store = _store(tmp_path)
    store.set_credentials(GogCookieJar([]), access_token="tok-gog")

    product = {
        "id": 1207658691,
        "downloads": {
            "installers": [
                {
                    "os": "windows",
                    "files": [
                        {"id": "en1", "size": 100,
                         "downlink": "https://api.gog.com/products/1207658691/downlink/installer/en1"},
                        {"id": "en2", "size": 200,
                         "downlink": "https://api.gog.com/products/1207658691/downlink/installer/en2"},
                    ],
                },
                {"os": "linux", "files": [
                    {"id": "ln", "size": 999,
                     "downlink": "https://api.gog.com/.../linux-big"},
                ]},
            ]
        },
    }
    resolve = {"downlink": "https://gog-cdn.example/ut2k4-setup.exe"}

    calls: list[str] = []

    def _fake_get(url, headers=None, timeout=None):  # noqa: ARG001
        calls.append(url)
        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self):
                # Product fetch is a plain /products/<id> GET; anything else
                # (the downlink resolver) returns the resolved file URL.
                wrapped = "/products/1207658691/downlink/" in url
                return product if not wrapped else resolve

        return _Resp()

    monkeypatch.setattr(gog_installer.requests, "get", _fake_get)
    url = gog_installer.offline_installer(store, "1207658691", "UT2004")
    assert url == "https://gog-cdn.example/ut2k4-setup.exe"
    assert any("installer/en2" in c for c in calls), "must pick the largest windows file"


def test_offline_installer_requires_auth(tmp_path: Path) -> None:
    from vitrine.sources.gog import installer as gog_installer

    store = _store(tmp_path)  # no credentials
    with pytest.raises(GogAuthError):
        gog_installer.offline_installer(store, "1207658691", "Game")


def test_game_from_product_preserves_installed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    """GOG's API reports no local install state; Vitrine's persisted flag wins."""
    from vitrine.sources import gog_source as gog_mod

    monkeypatch.setattr(gog_mod.paths, "secret_dir", lambda: tmp_path)
    src = GogSource(library)  # type: ignore[call-arg]
    src.user_id = "u"

    # Previously installed via offline installer -> stays installed on refresh.
    game = src._game_from_product({"id": "1", "title": "Hunie Pop", "slug": "hunie_pop"})
    assert game is not None and game.installed is False

    library.add(Game(name="Hunie Pop", source="gog", source_id="1", installed=True))
    again = src._game_from_product({"id": "1", "title": "Hunie Pop", "slug": "hunie_pop"})
    assert again is not None and again.installed is True


# -- gogdl bridge -------------------------------------------------------------


def test_gogdl_binary_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    monkeypatch.setenv(gogdl_mod.GOGDL_ENV, "/opt/gogdl")
    assert gogdl_mod.gogdl_binary() == "/opt/gogdl"
    assert gogdl_mod.is_installed()


def test_gogdl_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    monkeypatch.delenv(gogdl_mod.GOGDL_ENV, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    with pytest.raises(gogdl_mod.GogdlError):
        gogdl_mod.gogdl_binary()


def test_gogdl_write_auth_config(tmp_path: Path) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod
    from vitrine.sources.gog.auth import GOG_CLIENT_ID

    store = GogTokenStore(tmp_path, "u")
    store.set_credentials(
        GogCookieJar([]),
        access_token="at",
        refresh_token="rt",
        expires_in=3600,
        fetched_at=1000,
    )
    out = tmp_path / "auth.json"
    gogdl_mod.write_auth_config(store, str(out))
    data = json.loads(out.read_text())
    cred = data[GOG_CLIENT_ID]
    assert cred["access_token"] == "at"
    assert cred["refresh_token"] == "rt"
    assert cred["expires_in"] == 3600
    assert cred["loginTime"] == 1000


def test_gogdl_download_command_and_install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    monkeypatch.setenv(gogdl_mod.GOGDL_ENV, "/opt/gogdl")
    cmd = gogdl_mod.download_command("1443428641", "/tmp/inst", "/tmp/auth.json", lang="en-US")
    assert cmd[0] == "/opt/gogdl"
    assert "--auth-config-path" in cmd and "/tmp/auth.json" in cmd
    assert "download" in cmd and "1443428641" in cmd
    assert "--path" in cmd and "/tmp/inst" in cmd
    assert "--skip-dlcs" in cmd


def test_gogdl_repair_uses_existing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    monkeypatch.setenv(gogdl_mod.GOGDL_ENV, "/opt/gogdl")
    monkeypatch.setenv(gogdl_mod.GOGDL_CONFIG_ENV, str(tmp_path / "config"))
    manifest = tmp_path / "config" / "manifests" / "1443428641"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"installDirectory": "Game"}')

    assert gogdl_mod.has_manifest("1443428641") is True
    cmd = gogdl_mod.repair_command("1443428641", "/tmp/inst/Game", "/tmp/auth.json")
    assert cmd[0] == "/opt/gogdl"
    assert cmd[4] == "1443428641"
    assert "repair" in cmd


def test_gogdl_finds_depot_executable_without_info_marker(tmp_path: Path) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    game_dir = tmp_path / "Game"
    game_dir.mkdir()
    (game_dir / "setup.exe").write_text("")
    (game_dir / "Game.exe").write_text("")
    assert gogdl_mod.find_executable(str(game_dir)) == str(game_dir / "Game.exe")


def test_gogdl_executable_from_info(tmp_path: Path) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    (tmp_path / "HuniePop.exe").write_text("")
    exe = gogdl_mod.executable_from_info(
        {"tasks": [{"category": "game", "path": "HuniePop.exe"}]}, str(tmp_path)
    )
    assert exe == str(tmp_path / "HuniePop.exe")
    assert gogdl_mod.executable_from_info({}, str(tmp_path)) is None


def test_gogdl_find_game_dir_nested(tmp_path: Path) -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    nested = tmp_path / "root" / "HuniePop"
    nested.mkdir(parents=True)
    (nested / "goggame-1443428641.info").write_text("{}")
    assert gogdl_mod.install_is_valid("1443428641", str(tmp_path / "root"))
    assert gogdl_mod.find_game_dir("1443428641", str(tmp_path / "root")) == str(nested)
    assert gogdl_mod.install_is_valid("999", str(tmp_path / "root")) is False


def test_gogdl_reports_already_downloaded() -> None:
    from vitrine.sources.gog import gogdl as gogdl_mod

    assert gogdl_mod.reported_nothing_to_do(["Downloading", "Nothing to do."]) is True
    assert gogdl_mod.reported_nothing_to_do(["Nothing to do. extra"]) is False
    assert gogdl_mod.reported_nothing_to_do(["Nothing to do"]) is False


def test_token_needs_refresh_logic(tmp_path: Path) -> None:
    store = GogTokenStore(tmp_path, "u")
    # Fresh token with refresh_token -> no refresh needed.
    store.set_credentials(GogCookieJar([]), access_token="at", refresh_token="rt", expires_in=3600,
                          fetched_at=int(time.time()) - 100)
    assert store.needs_refresh() is False
    # Token near expiry -> needs refresh.
    store.set_credentials(GogCookieJar([]), access_token="at", refresh_token="rt", expires_in=3600,
                          fetched_at=int(time.time()) - 3590)
    assert store.needs_refresh() is True


def test_apply_refreshed_preserves_user_and_rotates(tmp_path: Path) -> None:
    store = GogTokenStore(tmp_path, "u1")
    store.set_credentials(GogCookieJar([]), access_token="old", refresh_token="oldrt", expires_in=100,
                          fetched_at=1)
    store.apply_refreshed({"access_token": "new", "refresh_token": "newrt", "expires_in": 2000})
    assert store.access_token() == "new"
    assert store.refresh_token() == "newrt"
    assert store.expires_in() == 2000
    assert store.load().get("user_id") == "u1"


def test_refresh_access_token_posts_and_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.gog import auth as gog_auth

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}

    def _fake_post(url, data, timeout):
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(gog_auth.requests, "post", _fake_post)
    out = gog_auth.refresh_access_token("RT")
    assert out["access_token"] == "at2"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "RT"

    class _Bad:
        status_code = 400

        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr(gog_auth.requests, "post", lambda *a, **k: _Bad())
    with pytest.raises(GogAuthError):
        gog_auth.refresh_access_token("BAD")


# -- local installed reconciliation --------------------------------------------

def _gog_data(tmp_path: Path) -> Path:
    depot = tmp_path / "gog"
    (depot / "huniepop-2" / "HuniePop").mkdir(parents=True)
    (depot / "huniepop-2" / "HuniePop" / "goggame-1443428641.info").write_text('{}')
    (depot / "huniepop-2" / "HuniePop" / "HuniePop.exe").write_bytes(b"MZ")
    return depot


def _gog_source(monkeypatch: pytest.MonkeyPatch, library, tmp_path: Path) -> GogSource:
    from vitrine.sources import gog_source as gog_mod

    monkeypatch.setattr(gog_mod.paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(gog_mod.paths, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(gog_mod.paths, "secret_dir", lambda: tmp_path)
    return GogSource(library)


def test_sync_installed_reconciles_on_disk(monkeypatch, library, tmp_path: Path) -> None:
    gog = _gog_source(monkeypatch, library, tmp_path)
    _gog_data(tmp_path)
    row = library.add(
        Game(name="HuniePop", slug="huniepop-2", source="gog",
             source_id="1443428641", installed=False)
    )

    from vitrine.sources.gog import gogdl

    monkeypatch.setattr(
        gogdl, "import_info",
        lambda _gid, _root, _auth: {"tasks": [{"category": "game", "path": "HuniePop.exe"}]},
    )

    assert gog.installed_on_disk() == {"1443428641"}
    gog.sync_installed()

    reloaded = library.game(row.id)
    assert reloaded.installed is True
    assert reloaded.executable is not None
    assert reloaded.executable.endswith("HuniePop.exe")


def test_dedupe_removes_stale_non_installed_twin(monkeypatch, library, tmp_path: Path) -> None:
    gog = _gog_source(monkeypatch, library, tmp_path)
    _gog_data(tmp_path)
    installed = library.add(
        Game(name="HuniePop", slug="huniepop-2", source="gog",
             source_id="1443428641", installed=True,
             executable=str(tmp_path / "gog" / "huniepop-2" / "HuniePop" / "HuniePop.exe"))
    )
    stale = library.add(
        Game(name="HuniePop", slug="huniepop", source="gog", source_id="339800", installed=False)
    )

    gog.sync_installed()

    assert library.game(installed.id) is not None
    assert library.game(stale.id) is None
    remaining = library.games(source="gog")
    assert [g.source_id for g in remaining] == ["1443428641"]


def test_sync_installed_never_downgrades(monkeypatch, library, tmp_path: Path) -> None:
    """An installed row stays installed even when the depot is not on disk."""
    gog = _gog_source(monkeypatch, library, tmp_path)
    row = library.add(
        Game(name="Vanished", slug="vanished", source="gog",
             source_id="999", installed=True,
             executable=str(tmp_path / "nowhere" / "game.exe"))
    )
    gog.sync_installed()
    assert library.game(row.id).installed is True
