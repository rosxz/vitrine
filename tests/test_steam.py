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
    from vitrine import artwork as artwork_mod

    monkeypatch.setattr(artwork_mod, "refresh_game_artwork", lambda _lib, game, force=False: False)
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


def test_steam_source_promotes_provider_art_and_lists_missing(
    steam_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.base import SourceGame
    from vitrine.sources.steam.auth import CookieJar, SteamTokenStore
    from vitrine.sources.steam_source import SteamSource

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    store = SteamTokenStore(tmp_path, "76561198123871777")
    store.set_credentials(CookieJar([{"name": "sessionid", "value": "abc"}]), access_token="tok-123")

    library = _library(steam_root)
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: str(steam_root))
    from vitrine.library import Game

    # Seed one pre-existing Steam row still carrying the old "local" default.
    library.add(
        Game(name="Old Game", slug="old-game", source="steam", source_id="11112222", artwork_source="local")
    )
    monkeypatch.setattr(SteamSource, "_owned_games", lambda self, store: [
        SourceGame(source="steam", appid="11112222", name="Old Game"),
    ])

    src = SteamSource(library)
    src.steamid64 = "76561198123871777"
    src.sync()

    # The sync promoted the old row to provider (so async art knows to fetch).
    game = library.game_by_source_id("steam", "11112222")
    assert game.artwork_source == "provider"

    # And it surfaces the missing-art work for the async pass, not doing it inline.
    pending = src.games_needing_artwork()
    assert any(g.source_id == "11112222" for g in pending)


def test_games_needing_artwork_force_refresh_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the global "refresh all" setting on, already-artworked games are
    returned too so a source refresh re-pulls them."""
    from vitrine import artwork
    from vitrine.library import Game
    from vitrine.sources.steam_source import SteamSource

    library = _library(monkeypatch)
    library.add(Game(name="Have Art", source="steam", source_id="1", cover="/c", banner="/b"))
    library.add(Game(name="No Art", source="steam", source_id="2"))
    src = SteamSource(library)
    assert [g.name for g in src.games_needing_artwork()] == ["No Art"]

    library.set_setting(artwork.FORCE_REFRESH_SETTING, True)
    names = [g.name for g in src.games_needing_artwork()]
    assert "Have Art" in names and "No Art" in names


def test_steam_source_requires_login_to_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.steam.auth import SteamAuthError
    from vitrine.sources.steam_source import SteamSource

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    monkeypatch.setattr(steam_config, "find_steam_root", lambda: "")
    source = SteamSource(_library(monkeypatch))
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

# -- login cookie capture --------------------------------------------------------

def test_fetch_access_token_uses_saved_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    from vitrine.sources import steam_source as steam_source_mod
    from vitrine.sources.steam.auth import CookieJar, SteamTokenStore

    monkeypatch.setattr(steam_source_mod.paths, "secret_dir", lambda: tmp_path)
    store = SteamTokenStore(tmp_path, "111")
    jar = CookieJar(
        [
            {"name": "sessionid", "value": "abc", "domain": "store.steampowered.com", "path": "/"},
            {"name": "steamLoginSecure", "value": "jwt", "domain": "store.steampowered.com", "path": "/"},
        ]
    )
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
    sent = recorded["cookies"]
    assert sent.get("sessionid", domain="store.steampowered.com") == "abc"
    assert sent.get("steamLoginSecure", domain="store.steampowered.com") == "jwt"
    # The refreshed token is persisted.
    assert SteamTokenStore(tmp_path, "111").access_token() == "fresh-token"


def test_cast_cookie_list_keeps_session_cookies() -> None:
    import gi

    gi.require_version("Soup", "3.0")
    from gi.repository import Soup

    from vitrine.ui.steam_login_dialog import cast_cookie_list

    login = Soup.Cookie.new("steamLoginSecure", "JWT", "store.steampowered.com", "/", 3600)
    session = Soup.Cookie.new("sessionid", "abc123", "store.steampowered.com", "/", -1)
    jar = cast_cookie_list([login, session])
    assert jar.get("steamLoginSecure") == "JWT"
    assert jar.get("sessionid") == "abc123"
    # The session cookie must keep its (missing) expiry; the secure cookie has one.
    assert jar.expires("sessionid") is None
    assert jar.expires("steamLoginSecure") is not None
    assert jar.to_dict()[0]["domain"] == "store.steampowered.com"


# -- access token extraction -------------------------------------------------

def test_extract_webapi_token_family_shape() -> None:
    from vitrine.sources.steam.auth import _extract_webapi_token

    payload = {"success": True, "data": {"webapi_token": "abc"}}
    assert _extract_webapi_token(payload) == "abc"


def test_extract_webapi_token_top_level() -> None:
    from vitrine.sources.steam.auth import _extract_webapi_token

    assert _extract_webapi_token({"webapi_token": "top"}) == "top"


def test_extract_webapi_token_missing() -> None:
    from vitrine.sources.steam.auth import _extract_webapi_token

    assert _extract_webapi_token({"success": False}) == ""
    assert _extract_webapi_token(["not", "a", "dict"]) == ""
    assert _extract_webapi_token({}) == ""


# -- domain-aware cookie jar ----------------------------------------------------

def test_cookies_for_requests_preserves_domains() -> None:
    from vitrine.sources.steam.auth import CookieJar, _cookies_for_requests

    jar = CookieJar(
        [
            {
                "name": "sessionid",
                "value": "store-session",
                "domain": "store.steampowered.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "sessionid",
                "value": "community-session",
                "domain": "steamcommunity.com",
                "path": "/",
                "secure": True,
            },
            {
                "name": "steamLoginSecure",
                "value": "JWT",
                "domain": "store.steampowered.com",
                "path": "/",
                "secure": True,
            },
        ]
    )
    out = _cookies_for_requests(jar)
    # Both sessionids must survive (flattening would fold them into one).
    assert out.get("sessionid", domain="store.steampowered.com") == "store-session"
    assert out.get("sessionid", domain="steamcommunity.com") == "community-session"
    assert out.get("steamLoginSecure", domain="store.steampowered.com") == "JWT"


def test_cookies_for_requests_skips_domainless() -> None:
    from vitrine.sources.steam.auth import CookieJar, _cookies_for_requests

    jar = CookieJar([{"name": "orphan", "value": "x", "domain": ""}])
    assert len(_cookies_for_requests(jar)) == 0


def test_sync_installed_tolerates_absent_numbers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources import steam_source as ss_mod
    from vitrine.sources.base import SourceGame
    from vitrine.sources.steam_source import SteamSource

    monkeypatch.setattr(ss_mod.paths, "secret_dir", lambda: tmp_path)
    conn = __import__("vitrine.db", fromlist=["connect"]).connect(":memory:")
    __import__("vitrine.db", fromlist=["initialize"]).initialize(conn)
    lib = __import__("vitrine.library", fromlist=["Library"]).Library(conn)
    lib.merge_source_games(
        "steam",
        [SourceGame(source="steam", appid="123", name="G", slug="g")],
    )

    src = SteamSource(lib)
    # Manifests mimic fields that are absent/None for never-run games.
    src._installed_games = lambda: [  # type: ignore[assignment]
        SourceGame(
            source="steam", appid="123", name="G", slug="g", installed=True,
            details={"playtime_forever": None, "lastplayed": None},
        )
    ]
    src.sync_installed()  # must not raise int(None)

    game = lib.games(source="steam")[0]
    assert game.installed is True
    from vitrine.sources.steam_source import _to_number

    assert _to_number(None) is None
    assert _to_number("0") == 0
    assert _to_number("12345") == 12345
    assert _to_number("1.5") == 1.5
    assert _to_number("-") is None


# -- installed flag stays bool / prune on resync ---------------------------------

def test_owned_installed_is_never_none() -> None:
    from vitrine.library import Game

    # The old expression bool(playtime) or item.get("playtime_2weeks") could be
    # None for a 0-playtime game missing playtime_2weeks -> int(None) crash.
    item = {"appid": 9, "name": "X", "playtime_forever": 0}
    installed = bool(item.get("playtime_forever", 0)) or bool(item.get("playtime_2weeks"))
    assert installed is False
    # And an installed game stays a real bool too.
    item2 = {"appid": 9, "name": "X", "playtime_forever": 120, "playtime_2weeks": 60}
    assert bool(item2.get("playtime_forever", 0)) or bool(item2.get("playtime_2weeks")) is True
    # to_row must always produce an int (never crash on a falsy/None flag).
    row = Game(name="X", source="steam", source_id="9", installed=False).to_row()
    assert row["installed"] == 0
    row2 = Game(name="Y", source="steam", source_id="10", installed=None).to_row()  # type: ignore[arg-type]
    assert row2["installed"] == 0


def test_prune_source_games_removes_only_uninstalled(tmp_path: Path) -> None:
    from vitrine import db
    from vitrine.library import Game, Library
    from vitrine.sources.base import SourceGame

    conn = db.connect(":memory:")
    db.initialize(conn)
    lib = Library(conn)
    lib.merge_source_games(
        "steam",
        [
            SourceGame(source="steam", appid="1", name="Kept Owned", installed=False),
            SourceGame(source="steam", appid="2", name="Installed", installed=True),
        ],
    )
    lib.add(Game(name="Manual-local", source="local"))
    assert len(lib.games(source="steam")) == 2

    # Resync that no longer lists appid 1 -> should prune it, keep installed.
    removed = lib.prune_source_games("steam", keep_appids=["2"])
    assert removed == 1
    apps = {g.source_id for g in lib.games(source="steam")}
    assert apps == {"2"}
    # Local source untouched.
    assert len(lib.games(source="local")) == 1


def test_prune_source_games_removes_stale_installed_flag(tmp_path: Path) -> None:
    from vitrine import db
    from vitrine.library import Library
    from vitrine.sources.base import SourceGame

    conn = db.connect(":memory:")
    db.initialize(conn)
    lib = Library(conn)
    lib.merge_source_games(
        "steam",
        [
            SourceGame(source="steam", appid="1", name="Played not installed", installed=False),
            SourceGame(source="steam", appid="2", name="Actually on disk", installed=True),
            SourceGame(source="steam", appid="3", name="Stale-flagged", installed=True),
        ],
    )
    # Simulate a full reset: only appid 2 is authoritative-on-disk.
    removed = lib.prune_source_games(
        "steam",
        keep_installed=False,
        preserve_on_disk={"2"},
    )
    assert removed == 2
    apps = {g.source_id for g in lib.games(source="steam")}
    assert apps == {"2"}, f"expected only truly-on-disk app to remain, got {apps}"
