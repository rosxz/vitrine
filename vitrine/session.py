"""Unified session tracking for running games and installs (GUI-free).

Vitrine launched games through several ad-hoc mechanisms (``Runtime`` for
owned processes, ``SteamSessionWatcher`` for Steam-owned ones, ``DownloadJob``
for installs), each with its own state slots in the window. The
:class:`SessionManager` and the :class:`Session` interface introduced here give
every "active thing" one consistent shape: it can be started, stopped, polled,
and reports elapsed time and per-line output. The window then tracks a single
running game through one object instead of three parallel state machines.

Callbacks fire from worker threads; GUI consumers marshal them (e.g. with
``GLib.idle_add``) as usual.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .library import Game

logger = logging.getLogger(__name__)

#: A process-backed activity (a running game, or an install) that can be started,
#: stopped and polled. This is the common contract ``Runtime`` (owned games),
#: ``SteamSessionWatcher`` (Steam-owned games) and ``DownloadJob`` (installs)
#: converge on.
class Session:
    def is_active(self) -> bool:
        """Whether the underlying process/session is still running."""
        raise NotImplementedError

    def stop(self, *, kill: bool = False) -> None:
        """Request graceful stop (or force-kill when ``kill`` is set)."""
        raise NotImplementedError

    def elapsed(self) -> float:
        """Seconds since the session became active, or 0.0."""
        return 0.0


@dataclass
class RunningSession:
    """A game that is currently running, with the session watching it.

    ``game`` is the library entry; ``session`` is the :class:`Session` driving
    it (owned ``Runtime``, Steam watcher, or an install job); ``started_monotonic``
    is the wall-clock reference used to report elapsed time and playtime.
    """

    game: Game
    session: Session
    started_monotonic: float = 0.0

    def elapsed(self) -> float:
        if not self.started_monotonic:
            return 0.0
        return time.monotonic() - self.started_monotonic


#: Invoked when a running game's session reports a line of output (optional).
OnLine = Callable[[Game, str], None]
#: Invoked when a running game ends. ``hours`` is elapsed playtime, ``returncode``
#: the process exit code (or ``None`` when unknown, e.g. Steam).
OnExit = Callable[[Game, float, int | None], None]


class SessionManager:
    """Tracks a single running game (and concurrent installs) uniformly.

    Replaces the window's three parallel state slots: only one game can be
    "running" at a time (enforced here), while any number of installs may be in
    progress alongside it. Consumers subscribe and receive events.
    """

    def __init__(
        self,
        *,
        on_exit: OnExit | None = None,
    ) -> None:
        self._active: RunningSession | None = None
        self._installs: dict[int, Session] = {}
        self.on_exit = on_exit

    # -- running game ----------------------------------------------------------

    @property
    def running_game(self) -> Game | None:
        return self._active.game if self._active is not None else None

    @property
    def active(self) -> RunningSession | None:
        return self._active

    def is_running(self, game: Game | None) -> bool:
        active = self._active
        if active is None or game is None:
            return False
        return active.game.id is not None and active.game.id == game.id

    def elapsed(self) -> float:
        return self._active.elapsed() if self._active is not None else 0.0

    def begin(self, game: Game, session: Session) -> None:
        """Start watching ``game`` as the single running game under ``session``.

        The caller is responsible for having already started the underlying
        process. If another game is running it is not auto-stopped; callers
        should check :meth:`running_game` / :meth:`is_busy` first.
        """
        self._active = RunningSession(game=game, session=session, started_monotonic=time.monotonic())
        logger.info("SessionManager: now running %s", game.name)

    def end(self, returncode: int | None = None) -> None:
        """End the running session, reporting elapsed playtime via ``on_exit``."""
        active = self._active
        self._active = None
        if active is None:
            return
        hours = active.elapsed() / 3600.0
        logger.info("SessionManager: %s ended (%.1f min)", active.game.name, hours * 60)
        if self.on_exit is not None:
            try:
                self.on_exit(active.game, hours, returncode)
            except Exception:  # noqa: BLE001
                logger.exception("on_exit handler failed for %s", active.game.name)

    def stop_running(self, *, kill: bool = True) -> bool:
        """Stop the running game's session and clear the running slot.

        Returns True if there was a running game. The active session is cleared
        immediately (the user asked to stop), matching how stopping a game used
        to reset the "Playing" state; ``on_exit`` is NOT fired for a stopped
        game (the elapsed time is discarded, not counted as playtime).
        """
        active = self._active
        if active is None:
            return False
        self._active = None
        logger.info("SessionManager: stopping %s", active.game.name)
        try:
            active.session.stop(kill=kill)
        except Exception:  # noqa: BLE001 - a session must not block the UI
            logger.exception("stop failed for %s", active.game.name)
        return True

    # -- installs --------------------------------------------------------------

    def add_install(self, game: Game, session: Session) -> None:
        if game.id is not None:
            self._installs[game.id] = session

    def install(self, game: Game) -> Session | None:
        if game.id is not None:
            return self._installs.get(game.id)
        return None

    def remove_install(self, game: Game) -> None:
        if game.id is not None:
            self._installs.pop(game.id, None)

    def installing(self, game: Game) -> bool:
        return game.id is not None and game.id in self._installs

    def stop_install(self, game: Game) -> None:
        session = self.install(game)
        if session is not None:
            try:
                session.stop(kill=True)
            except Exception:  # noqa: BLE001
                logger.exception("stop install failed for %s", game.name)

    # -- combined state --------------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while a game is running or any install is in progress."""
        return self._active is not None or bool(self._installs)