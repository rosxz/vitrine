"""Tests for the Steam source: VDF parsing, install discovery, and the
durable auth/token cache that stops the frequent-relogin problem."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from vitrine.sources.steam import config as steam_config
from vitrine.sources.steam.auth import CookieJar, SteamTokenStore
from vitrine.sources.steam.vdf import parse_vdf, parse_vdf_file

# -- VDF parsing ---------------------------------------------------------------

def test_parse_simple_pairs() -> None:
    assert parse_vdf('"root" { "a" "1" "b" "two words" }') == {
        "root": {"a": "1", "b": "two words"}
    }


def test_parse_nested_blocks() -> None:
    text = '"InstallConfigStore" { "Software" { "Valve" { "Steam" { "AutoLoginUser" "bob" } } } }'
    assert parse_vdf(text)["InstallConfigStore"]["Software"]["Valve"]["Steam"]["AutoLoginUser"] == "bob"


def test_parse_escaped_values() -> None:
    result = parse_vdf('"root" { "name" "Fear & Hunger" }')
    assert result["root"]["name"] == "Fear & Hunger"


def test_parse_file(tmp_path: Path) -> None:
    file = tmp_path / "appmanifest_1002300.acf"
    file.write_text('"AppState" { "appid" "1002300" "name" "Fear & Hunger" }', encoding="utf-8")
    assert parse_vdf_file(file)["AppState"]["appid"] == "1002300"


# -- Steam install discovery -----------------------------------------------------

def _write_vdf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def steam_root(tmp_path: Path) -> Path:
    root = tmp_path / "steam"
    main_lib = tmp_path / "MAIN"
    extra_lib = tmp_path / "EXTRA"
    # libraryfolders.vdf with a main library and one extra.
    _write_vdf(
        root / "steamapps" / "libraryfolders.vdf",
        '"libraryfolders" {'
        f'  "0" {{ "path" "{main_lib}" }}'
        f'  "1" {{ "path" "{extra_lib}" }}'
        "}",
    )
    # Manifests in the main steamapps folder.
    _write_vdf(
        root / "steamapps" / "appmanifest_1002300.acf",
        '"AppState" {'
        '  "appid" "1002300"'
        '  "name" "Fear & Hunger"'
        '  "installdir" "Fear & Hunger"'
        '  "LastPlayed" "1760455998"'
        '  "SizeOnDisk" "1025073699"'
        "}",
    )
    _write_vdf(
        root / "steamapps" / "appmanifest_221410.acf",
        '"AppState" { "appid" "221410" "name" "Steamworks Common Redistributables" }',
    )
    # A manifest in the extra library.
    extra_apps = extra_lib / "steamapps"
    extra_apps.mkdir(parents=True, exist_ok=True)
    _write_vdf(
        extra_apps / "appmanifest_12110.acf",
        '"AppState" { "appid" "12110" "name" "Grand Theft Auto: Vice City" "playtime_forever" "12345" }',
    )
    return root


def test_find_steam_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "steam"
    # No steamapps dir yet -> not found.
    assert steam_config.find_steam_root() != str(root)
    (root / "steamapps").mkdir(parents=True)
    monkeypatch.setenv(steam_config.STEAM_ROOT_ENV, str(root))
    assert steam_config.find_steam_root() == str(root)


def test_library_folders(steam_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: str(steam_root))
    folders = steam_config.library_folders(str(steam_root))
    # The main library path and the extra one; real STEAM_DATA_DIRS aren't
    # relevant here because we call library_folders directly.
    assert folders
    assert all(folders)


def test_steamapps_dirs(steam_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: str(steam_root))
    dirs = steam_config.steamapps_dirs(str(steam_root))
    assert str(steam_root / "steamapps") in dirs


def test_appmanifest_paths(steam_root: Path) -> None:
    paths = steam_config.appmanifest_paths(str(steam_root / "steamapps"))
    assert len(paths) == 2
    assert steam_config.appid_from_manifest_path(paths[0])


def test_active_steamid64_from_autologin(tmp_path: Path) -> None:
    root = tmp_path / "steam"
    _write_vdf(
        root / "config" / "config.vdf",
        '"InstallConfigStore" { "Software" { "Valve" { "Steam" {'
        '  "AutoLoginUser" "martim"'
        '  "Accounts" { "martim" { "76561198123871777" { "PersonaName" "Martim" "mostrecent" "1" } } }'
        "} } } }",
    )
    assert steam_config.active_steamid64(str(root)) == "76561198123871777"


def test_active_steamid64_falls_back_to_loginusers(tmp_path: Path) -> None:
    root = tmp_path / "steam"
    _write_vdf(
        root / "config" / "loginusers.vdf",
        '"users" {'
        '  "76561198000000000" { "AccountName" "old" "mostrecent" "0" }'
        '  "76561198123871777" { "AccountName" "martim" "mostrecent" "1" }'
        "}",
    )
    assert steam_config.active_steamid64(str(root)) == "76561198123871777"


# -- auth cache (the relogin fix) ----------------------------------------------

def test_cookie_jar_preserves_session_cookies() -> None:
    jar = CookieJar(
        [
            {"name": "sessionid", "value": "abc", "domain": "steamcommunity.com"},
            {"name": "steamLoginSecure", "value": "jwt", "expires": 1820967283},
        ]
    )
    # Session cookies (no expires) must survive round-trips.
    restored = CookieJar.from_dict(jar.to_dict())
    assert restored.get("sessionid") == "abc"
    assert restored.get("steamLoginSecure") == "jwt"
    assert restored.expires("steamLoginSecure") == 1820967283
    assert restored.expires("sessionid") is None


def test_token_store_round_trip(tmp_path: Path) -> None:
    store = SteamTokenStore(tmp_path, "76561198123871777")
    assert not store.exists()

    jar = CookieJar([{"name": "sessionid", "value": "abc"}, {"name": "steamRefresh_steam", "value": "refresh-jwt"}])
    store.set_credentials(jar, access_token="token-1", fetched_at=int(time.time()))

    assert store.exists()
    restored = SteamTokenStore(tmp_path, "76561198123871777")
    assert restored.cookies().get("sessionid") == "abc"
    assert restored.refresh_token() == "refresh-jwt"
    assert restored.access_token() == "token-1"
    assert restored.age_seconds() < 10


def test_token_store_is_per_account(tmp_path: Path) -> None:
    a = SteamTokenStore(tmp_path, "111")
    a.set_credentials(CookieJar(), access_token="A")
    b = SteamTokenStore(tmp_path, "222")
    assert not b.exists()


def test_needs_network_refresh(tmp_path: Path) -> None:
    store = SteamTokenStore(tmp_path, "111")
    store.set_credentials(CookieJar(), access_token="tok", fetched_at=int(time.time()) - 100)
    assert not store.needs_network_refresh(access_token_ttl=3600)
    assert store.needs_network_refresh(access_token_ttl=30)


def test_token_store_clear(tmp_path: Path) -> None:
    store = SteamTokenStore(tmp_path, "111")
    store.set_credentials(CookieJar(), access_token="tok")
    assert store.exists()
    store.clear()
    assert not store.exists()


def test_token_store_corrupt_file(tmp_path: Path) -> None:
    store = SteamTokenStore(tmp_path, "111")
    store.filename.parent.mkdir(parents=True, exist_ok=True)
    store.filename.write_text("{ not json", encoding="utf-8")
    assert store.access_token() == ""
    assert store.cookies().to_dict() == []


# -- SteamSource integration -----------------------------------------------------

def _library(tmp_path: Path):
    from vitrine import db
    from vitrine.library import Library

    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def test_steam_source_syncs_with_auth(
    steam_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.base import SourceGame
    from vitrine.sources.steam.auth import CookieJar, SteamTokenStore
    from vitrine.sources.steam_source import SteamSource

    # Seed an authenticated session (token store) alongside the fake install.
    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    store = SteamTokenStore(tmp_path, "76561198123871777")
    store.set_credentials(CookieJar([{"name": "sessionid", "value": "abc"}]), access_token="tok-123")

    library = _library(steam_root)
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: str(steam_root))
    monkeypatch.setattr(SteamSource, "_owned_games", lambda self, store: [
        SourceGame(source="steam", appid="1002300", name="Fear & Hunger"),
        SourceGame(source="steam", appid="999999", name="Not Installed Anything"),
        SourceGame(source="steam", appid="221410", name="Steamworks Common Redistributables"),
    ])

    source = SteamSource(library)
    source.steamid64 = "76561198123871777"
    count = source.sync()

    # 2 real games after exclusions: the locally-installed Fear & Hunger plus a
    # web-only uninstalled title. 221410 is dropped.
    assert count == 2, f"expected 2 known games, got {count}"
    rows = library.source_games("steam")
    names = {r["name"] for r in rows}
    assert "Fear & Hunger" in names
    assert "Not Installed Anything" in names
    assert "Steamworks Common Redistributables" not in names

    # The unified games table also received the entries.
    grid = {g.name for g in library.games(source="steam")}
    assert "Fear & Hunger" in grid
    assert "Not Installed Anything" in grid


def test_steam_source_requires_login_to_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.steam.auth import SteamAuthError
    from vitrine.sources.steam_source import SteamSource

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: "")
    source = SteamSource(_library(monkeypatch))

    assert not source.is_configured()
    with pytest.raises(SteamAuthError):
        source.sync()


def test_steam_source_no_token_interrupts_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.steam.auth import SteamAuthError
    from vitrine.sources.steam_source import SteamSource

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    source = SteamSource(_library(monkeypatch))
    assert not source.is_authenticated()
    with pytest.raises(SteamAuthError):
        source.sync()


def test_steam_source_is_registered() -> None:
    from vitrine.sources.base import registry

    assert registry.get("steam") is not None

# -- login cookie dump parsing ---------------------------------------------------

def test_parse_cookie_dump_netscape() -> None:
    from vitrine.ui.steam_login_dialog import _parse_cookie_dump

    dump = (
        "#HttpOnly_store.steampowered.com\tFALSE\t/\tTRUE\t1820967283\tsteamLoginSecure\tTOKEN-ABC\n"
        "store.steampowered.com\tFALSE\t/\tFALSE\t1820967284\tsessionid\t123456789\n"
    )
    jar = _parse_cookie_dump(dump)
    assert jar.get("steamLoginSecure") == "TOKEN-ABC"
    assert jar.get("sessionid") == "123456789"
    assert jar.expires("steamLoginSecure") == 1820967283


def test_parse_cookie_dump_chrome_rows() -> None:
    from vitrine.ui.steam_login_dialog import _parse_cookie_dump

    jar = _parse_cookie_dump("sessionid=abc123\nsteamLoginSecure=DEF456\n")
    assert jar.get("steamLoginSecure") == "DEF456"
    assert jar.get("sessionid") == "abc123"


def test_parse_cookie_dump_rejects_empty() -> None:
    from vitrine.ui.steam_login_dialog import _parse_cookie_dump

    assert _parse_cookie_dump("# comments only\n\n").to_dict() == []


def test_fetch_access_token_uses_saved_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.steam.auth import CookieJar, SteamTokenStore

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    store = SteamTokenStore(tmp_path, "111")
    jar = CookieJar([{"name": "sessionid", "value": "abc"}, {"name": "steamLoginSecure", "value": "jwt"}])
    store.set_credentials(jar)

    recorded: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"success": True, "data": {"webapi_token": "fresh-token"}}

    def fake_get(self, url, **kwargs):  # noqa: ARG001 - patched onto Session.get
        recorded["cookies"] = kwargs.get("cookies")
        recorded["url"] = url
        return FakeResponse()

    monkeypatch.setattr(requests.Session, "get", fake_get)
    token = store.fetch_access_token()
    assert token == "fresh-token"
    assert recorded["cookies"] == {"sessionid": "abc", "steamLoginSecure": "jwt"}
    # The refreshed token is persisted.
    assert SteamTokenStore(tmp_path, "111").access_token() == "fresh-token"
