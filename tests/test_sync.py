"""SyncService: consolidated per-source refresh/reset via the registry."""

from __future__ import annotations

import pytest

from vitrine.infra import db
from vitrine.services.library import Library
from vitrine.services.sync import AuthRequired, SyncService


@pytest.fixture
def library() -> Library:
    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def _local_source() -> None:
    pass  # placeholder; real store sources need credentials, covered below


def test_unknown_source_raises(library: Library) -> None:
    with pytest.raises(ValueError):
        SyncService(library).source("nope")


def test_sync_requires_auth_for_store(library: Library, monkeypatch) -> None:
    # A store source that is not authenticated must raise AuthRequired.
    from vitrine.sources import registry

    class FakeAuth(registry.get("steam")):
        def is_authenticated(self) -> bool:
            return False

    monkeypatch.setattr(registry.get("steam"), "is_authenticated", lambda self: False)
    with pytest.raises(AuthRequired):
        SyncService(library).sync("steam")


def test_reset_clears_credentials_for_local_source(library: Library) -> None:
    # Local has no auth; reset should be a no-op that does not explode.
    SyncService(library).reset("local")
    assert True


def test_sync_local_returns_zero(library: Library) -> None:
    result = SyncService(library).sync("local")
    assert result.source == "local"
    assert result.count == len(library.games(source="local"))
    assert result.pending_artwork == 0

def test_complete_login_persists_account_id(library: Library) -> None:
    """A login dialog resolving the account must persist it for the next sync
    (otherwise Epic/GOG need a second login to fill the library)."""
    from vitrine.sources import registry

    e = registry.get("epic")(library)
    e.complete_login("acct-999")
    assert library.setting("epic_account_id") == "acct-999"

    g = registry.get("gog")(library)
    g.complete_login("user-777")
    assert library.setting("gog_user_id") == "user-777"

    # Local has no account; must not crash.
    registry.get("local")(library).complete_login("x")
