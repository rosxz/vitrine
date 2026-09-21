"""Tests for the Epic source: credential store, legendary wrapper, manifests,
and the sync pipeline (with a mocked legendary binary)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vitrine.sources.epic.auth import EpicAuthError, EpicTokenStore, exchange_code_for_token
from vitrine.sources.epic_source import ACCOUNT_SETTING, extract_account

# -- token store ---------------------------------------------------------------


def _store(tmp_path: Path, account_id: str = "abc123") -> EpicTokenStore:
    return EpicTokenStore(tmp_path, account_id)


def test_epic_token_store_round_trip_and_clear(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not store.exists()
    store.set_credentials("code-1", {"access_token": "tok-1"})
    assert store.exists()
    assert store.code() == "code-1"
    assert store.access_token() == "tok-1"
    assert store.is_authenticated() is True

    store.clear()
    assert not store.exists()
    assert store.is_authenticated() is False


def test_epic_token_store_code_only_counts_as_authenticated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_credentials("code-1")
    assert store.is_authenticated() is True


# -- code exchange -------------------------------------------------------------

def test_exchange_code_posts_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import auth as epic_auth

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok-abc", "account_id": "acct-1"}

    def _fake_post(url, data=None, auth=None, headers=None, timeout=None):  # noqa: ARG001
        captured["url"] = url
        captured["data"] = data
        captured["auth"] = auth
        return _Resp()

    monkeypatch.setattr(epic_auth.requests, "post", _fake_post)
    result = epic_auth.exchange_code_for_token("mycode")

    assert result["access_token"] == "tok-abc"
    assert captured["url"] == epic_auth.EPIC_TOKEN_URL
    assert captured["data"]["grant_type"] == "exchange_code"
    assert captured["data"]["exchange_code"] == "mycode"
    assert captured["data"]["token_type"] == "eg1"
    # The client id/secret are sent as HTTP Basic auth.
    assert captured["auth"][0] == epic_auth.EPIC_CLIENT_ID
    assert captured["auth"][1] == epic_auth.EPIC_CLIENT_SECRET


def test_authorization_code_for_token_uses_code_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vitrine.sources.epic import auth as epic_auth

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok-authz"}

    def _fake_post(url, data=None, auth=None, headers=None, timeout=None):  # noqa: ARG001
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(epic_auth.requests, "post", _fake_post)
    epic_auth.authorization_code_for_token("acode")
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "acode"


def test_exchange_code_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import auth as epic_auth

    class _Resp:
        status_code = 400

        def json(self):
            return {"errorCode": "errors.com.epicgames.oauth.invalid_grant"}

    monkeypatch.setattr(epic_auth.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(EpicAuthError, match="400"):
        exchange_code_for_token("bad")


def test_obtain_token_falls_back_to_authorization_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login code may arrive as an exchange code or an authorization code."""
    from vitrine.sources.epic import auth as epic_auth

    calls: list[str] = []

    def _exchange(code):  # noqa: ARG001
        calls.append("exchange")
        raise epic_auth.EpicAuthError("no such exchange code")

    def _authz(code):  # noqa: ARG001
        calls.append("authz")
        return {"access_token": "tok-z", "account_id": "a"}

    monkeypatch.setattr(epic_auth, "exchange_code_for_token", _exchange)
    monkeypatch.setattr(epic_auth, "authorization_code_for_token", _authz)

    result = epic_auth.obtain_token("somecode")
    assert result["access_token"] == "tok-z"
    assert calls == ["exchange", "authz"]


def test_login_url_embeds_redirect_target() -> None:
    """The login URL must carry the /id/api/redirect target (Lutris approach)."""
    from urllib.parse import parse_qs, urlparse

    from vitrine.sources.epic import auth as epic_auth

    parsed = urlparse(epic_auth.EPIC_AUTH_URL)
    assert parsed.path == "/id/login"
    redirect_url = parse_qs(parsed.query)["redirectUrl"][0]
    assert redirect_url.startswith("https://www.epicgames.com/id/api/redirect")
    assert "responseType=code" in redirect_url


def test_login_succeeds_via_http_exchange_without_legendary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login must work from our own HTTP exchange even if legendary is absent."""
    from vitrine.sources.epic import auth as epic_auth
    from vitrine.sources.epic import legendary as lg
    from vitrine.sources.epic.auth import EpicTokenStore
    from vitrine.ui.epic_login_dialog import EpicLoginDialog

    monkeypatch.setattr(lg, "is_installed", lambda: False)
    monkeypatch.setattr(lg, "auth", lambda code: None)

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "tok-real", "account_id": "acct-real"}

    monkeypatch.setattr(epic_auth.requests, "post", lambda *a, **k: _Resp())

    finished: dict = {}

    def _complete(ok: bool, account_id: str | None, code: str):
        finished.update(ok=ok, account_id=account_id, code=code)

    dialog = EpicLoginDialog(
        EpicTokenStore("/tmp/vitrine-epic-test-http", "abc"),
        on_complete=_complete,
    )
    dialog._exchange_code("REAL_AUTHZ_CODE_1234567890")
    assert finished["ok"] is True
    assert finished["account_id"] == "acct-real"


def test_epic_login_dialog_extracts_auth_body() -> None:
    """The redirect page body's authorizationCode is read for the exchange."""
    from vitrine.ui.epic_login_dialog import _extract_code_from_body

    body = (
        '{"authorizationCode":"AUTHZ_1234567890","exchangeCode":null,'
        '"redirectUrl":"https://localhost/launcher/authorized"}'
    )
    assert _extract_code_from_body(body) == "AUTHZ_1234567890"
    assert _extract_code_from_body('{"exchangeCode":"EXCH_1"}') == "EXCH_1"
    assert _extract_code_from_body('{"sid":"nothing_here"}') == ""


def test_extract_account_from_token() -> None:
    assert extract_account({"account_id": "x"}) == "x"
    assert extract_account({"sub": "y"}) == "y"
    assert extract_account({}) == ""


# -- legendary wrapper ---------------------------------------------------------

def test_legendary_binary_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import legendary as lg

    monkeypatch.delenv(lg.LEGENDARY_ENV, raising=False)
    monkeypatch.setattr(lg.shutil, "which", lambda _name: None)
    assert lg.is_installed() is False
    with pytest.raises(lg.LegendaryError):
        lg.legendary_binary()


def test_list_games_parses_legendary_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import legendary as lg

    payload = {
        "game": [
            {"app_name": "Eider", "title": "HITMAN 3", "slug": "hitman-3", "installed": True},
            {"app_name": "wog", "title": "World of Goo", "installed": False},
        ]
    }
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(lg, "_run", lambda *a, **k: proc)

    games = lg.list_games()
    assert len(games) == 2
    assert games[0]["app_name"] == "Eider"
    assert games[0]["title"] == "HITMAN 3"


def test_auth_passes_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import legendary as lg

    seen: list = []
    monkeypatch.setattr(
        lg, "_run", lambda args, timeout: (seen.append(list(args)) or _ok_proc())
    )
    lg.auth("code999")
    assert seen == [["auth", "--token", "code999"]]


def _ok_proc() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# -- config / manifests --------------------------------------------------------

def test_installed_manifests_reads_item_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import config as epic_config

    manifests = tmp_path / "Manifests"
    manifests.mkdir(parents=True)
    (manifests / "1.item").write_text(
        json.dumps({"AppName": "Eider", "DisplayName": "HITMAN 3", "bIsInstalled": True})
    )
    (manifests / "2.item").write_text(json.dumps({"AppName": "wog", "DisplayName": "World of Goo"}))
    (manifests / "3.item").write_text(json.dumps({"AppName": "nope", "bIsInstalled": False}))
    monkeypatch.setattr(epic_config, "egs_manifests_dir", lambda: manifests)

    installed = epic_config.installed_manifests()
    apps = {m["AppName"] for m in installed}
    assert apps == {"Eider", "wog"}


def test_egs_manifests_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.sources.epic import config as epic_config

    manifests = tmp_path / "Manifests"
    manifests.mkdir(parents=True)
    monkeypatch.setattr(epic_config, "EGS_MANIFESTS_ENV", "VITRINE_EGS_MANIFESTS")
    monkeypatch.setenv("VITRINE_EGS_MANIFESTS", str(manifests))

    assert epic_config.egs_manifests_dir() == manifests


# -- source sync ---------------------------------------------------------------

@pytest.fixture
def library() -> object:
    from vitrine import db
    from vitrine.library import Library

    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def test_sync_uses_legendary_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    from vitrine.sources import epic_source as epic_mod

    monkeypatch.setattr(epic_mod.paths, "secret_dir", lambda: tmp_path)
    store = EpicTokenStore(tmp_path, "abc123")
    store.set_credentials("code-1", {"access_token": "tok-1"})
    monkeypatch.setattr(library, "set_setting", lambda _k, _v: None)

    monkeypatch.setattr(
        epic_mod.lg,
        "list_games",
        lambda: [
            {"app_name": "Eider", "title": "HITMAN 3", "slug": "hitman-3", "installed": True},
            {"app_name": "wog", "title": "World of Goo"},
        ],
    )
    monkeypatch.setattr(epic_mod.lg, "list_installed", lambda: [])
    monkeypatch.setattr(epic_mod.lg, "is_installed", lambda: True)

    src = epic_mod.EpicSource(library)  # type: ignore[call-arg]
    src.account_id = "abc123"
    count = src.sync()
    assert count == 2

    names = {g.name for g in library.games(source="epic")}
    assert "HITMAN 3" in names
    assert "World of Goo" in names

    hitman = library.game_by_source_id("epic", "Eider")
    assert hitman is not None
    assert hitman.artwork_source == "lutris"


def test_sync_falls_back_to_http_when_legendary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    from vitrine.sources import epic_source as epic_mod
    from vitrine.sources.epic import legendary as lg

    monkeypatch.setattr(epic_mod.paths, "secret_dir", lambda: tmp_path)
    store = EpicTokenStore(tmp_path, "abc123")
    store.set_credentials("code-1", {"access_token": "tok-1"})
    monkeypatch.setattr(library, "set_setting", lambda _k, _v: None)

    # legendary is present but its listing fails (e.g. a bare install).
    monkeypatch.setattr(lg, "is_installed", lambda: True)
    monkeypatch.setattr(lg, "list_games", lambda: (_ for _ in ()).throw(lg.LegendaryError("no")))
    monkeypatch.setattr(lg, "list_installed", lambda: [])

    # The HTTP fallback enumerates owned entitlements without a game binary.
    from vitrine.sources.epic import auth as epic_auth

    monkeypatch.setattr(
        epic_auth, "list_owned", lambda token: [
            {"appName": "Eider", "title": "HITMAN 3", "slug": "hitman-3"},
            {"appName": "wog", "title": "World of Goo"},
        ]
    )

    src = epic_mod.EpicSource(library)  # type: ignore[call-arg]
    src.account_id = "abc123"
    count = src.sync()
    assert count == 2
    names = {g.name for g in library.games(source="epic")}
    assert "HITMAN 3" in names
    assert "World of Goo" in names


def test_sync_falls_back_to_http_when_legendary_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object
) -> None:
    from vitrine.sources import epic_source as epic_mod
    from vitrine.sources.epic import legendary as lg

    monkeypatch.setattr(epic_mod.paths, "secret_dir", lambda: tmp_path)
    store = EpicTokenStore(tmp_path, "abc123")
    store.set_credentials("code-1", {"access_token": "tok-1"})
    monkeypatch.setattr(library, "set_setting", lambda _k, _v: None)

    # legendary is not installed at all.
    monkeypatch.setattr(lg, "is_installed", lambda: False)
    monkeypatch.setattr(lg, "list_games", lambda: (_ for _ in ()).throw(lg.LegendaryError("not installed")))
    monkeypatch.setattr(lg, "list_installed", lambda: [])

    from vitrine.sources.epic import auth as epic_auth

    monkeypatch.setattr(
        epic_auth, "list_owned", lambda token: [{"appName": "wog", "title": "World of Goo"}]
    )

    src = epic_mod.EpicSource(library)  # type: ignore[call-arg]
    src.account_id = "abc123"
    assert src.sync() == 1
    names = {g.name for g in library.games(source="epic")}
    assert "World of Goo" in names


def test_sync_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: object) -> None:
    from vitrine.sources import epic_source as epic_mod

    monkeypatch.setattr(epic_mod.paths, "secret_dir", lambda: tmp_path)
    monkeypatch.setattr(epic_mod.lg, "is_installed", lambda: True)
    monkeypatch.setattr(epic_mod.lg, "list_games", lambda: [])

    src = epic_mod.EpicSource(library)  # type: ignore[call-arg]
    with pytest.raises(EpicAuthError):
        src.sync()


def test_account_setting_key() -> None:
    assert ACCOUNT_SETTING == "epic_account_id"

def test_legendary_binary_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flake wrapper sets VITRINE_LEGENDARY to an absolute path; the app
    must honour it even when 'legendary' is not on PATH."""
    from vitrine.sources.epic import legendary as lg

    monkeypatch.setenv(lg.LEGENDARY_ENV, "/nix/store/legendary/bin/legendary")
    monkeypatch.setattr(lg.shutil, "which", lambda _name: None)  # not on PATH
    assert lg.legendary_binary() == "/nix/store/legendary/bin/legendary"
    assert lg.is_installed() is True
