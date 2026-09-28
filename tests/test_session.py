"""SessionManager: the unified single-running-game + install coordinator."""

from __future__ import annotations

import time

from vitrine.library import Game
from vitrine.session import Session, SessionManager


class _StubSession(Session):
    def __init__(self, active: bool = True) -> None:
        self._active = active
        self.stopped = False
        self.stopped_with_kill = False

    def is_active(self) -> bool:
        return self._active

    def stop(self, *, kill: bool = False) -> None:
        self.stopped = True
        self.stopped_with_kill = kill
        self._active = False


def _game(name: str = "G", game_id: int = 1) -> Game:
    return Game(name=name, id=game_id, source="local")


def test_begin_and_running_game() -> None:
    mgr = SessionManager()
    game = _game()
    session = _StubSession()
    mgr.begin(game, session)
    assert mgr.running_game is game
    assert mgr.is_running(game)
    assert not mgr.is_running(_game("other", 2))
    assert mgr.busy


def test_begin_returns_elapsed_seconds() -> None:
    mgr = SessionManager()
    mgr.begin(_game(), _StubSession())
    time.sleep(0.01)
    assert 0.0 < mgr.elapsed() < 5.0


def test_end_reports_playtime_via_on_exit() -> None:
    exits: list[tuple[Game, float, int | None]] = []
    mgr = SessionManager(on_exit=lambda g, h, rc: exits.append((g, h, rc)))
    game = _game()
    mgr.begin(game, _StubSession())
    assert mgr.running_game is game
    mgr.end(returncode=0)
    assert mgr.running_game is None
    assert not mgr.busy
    assert len(exits) == 1
    assert exits[0][0] is game
    assert 0.0 <= exits[0][1] < 5.0
    assert exits[0][2] == 0


def test_end_with_no_active_is_noop() -> None:
    mgr = SessionManager()
    mgr.end(returncode=0)  # must not raise


def test_stop_running_dispatches_to_session() -> None:
    mgr = SessionManager()
    session = _StubSession()
    mgr.begin(_game(), session)
    assert mgr.stop_running(kill=True) is True
    assert session.stopped and session.stopped_with_kill
    assert mgr.running_game is None


def test_stop_running_with_nothing_returns_false() -> None:
    mgr = SessionManager()
    assert mgr.stop_running() is False


def test_installs_tracked_by_game_id() -> None:
    mgr = SessionManager()
    game = _game(game_id=42)
    install = _StubSession()
    mgr.add_install(game, install)
    assert mgr.install(game) is install
    assert mgr.installing(game)
    assert not mgr.installing(_game("other", 99))
    mgr.remove_install(game)
    assert not mgr.installing(game)
    assert not mgr.busy


def test_stop_install_kills_session() -> None:
    mgr = SessionManager()
    game = _game(game_id=7)
    install = _StubSession()
    mgr.add_install(game, install)
    mgr.stop_install(game)
    assert install.stopped and install.stopped_with_kill


def test_running_game_ignores_unknown_none() -> None:
    mgr = SessionManager()
    assert mgr.running_game is None
    assert mgr.is_running(None) is False
    assert not mgr.busy