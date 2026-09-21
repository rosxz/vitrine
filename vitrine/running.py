"""Process supervision: start, watch and stop launched games.

The runtime allows a single game to be running at a time. A game is tracked
from the moment its command is spawned until the watched process exits, at
which point the elapsed wall-clock time is reported as playtime in hours.

All callbacks fire from a background thread, so a GUI consumer must marshal
them back onto the main loop (e.g. with ``GLib.idle_add``).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable

from . import launch
from .library import Game

logger = logging.getLogger(__name__)

#: Callback signature for a game that has started.
OnStart = Callable[[Game], None]
#: Callback signature for a game that has exited.
OnExit = Callable[[Game, float, int], None]  # (game, playtime_hours, returncode)


class GameAlreadyRunning(RuntimeError):
    """Raised when a game is launched while another one is still running."""


class Runtime:
    """Tracks at most one running game and reports its lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._game: Game | None = None
        self._started_at = 0.0
        self.on_start: OnStart | None = None
        self.on_exit: OnExit | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None

    @property
    def running_game(self) -> Game | None:
        with self._lock:
            return self._game

    def start(self, game: Game, config: dict, runners_store: dict[str, str] | None = None) -> None:
        """Launch ``game`` under ``config`` and begin watching it.

        ``runners_store`` maps runner ids to their wine-binary paths so the
        selected runner can be resolved. Raises :class:`GameAlreadyRunning` if
        another game is still running.
        """
        with self._lock:
            if self._process is not None:
                name = self._game.name if self._game else "another game"
                raise GameAlreadyRunning(f"{name} is still running")

            process = launch.launch(launch.build_launch_plan(game, config, runners_store))
            self._process = process
            self._game = game
            self._started_at = time.monotonic()

        logger.info("Started %s (pid %s)", game.name, process.pid)
        if self.on_start is not None:
            try:
                self.on_start(game)
            except Exception:
                logger.exception("on_start handler failed for %s", game.name)

        threading.Thread(target=self._watch, args=(process, game), daemon=True, name="game-watch").start()

    def stop(self) -> None:
        """Terminate the running game's process tree, if any."""
        with self._lock:
            process = self._process
        if process is not None:
            logger.info("Stopping %s", self._game.name if self._game else "game")
            process.terminate()

    def _watch(self, process: subprocess.Popen, game: Game) -> None:
        try:
            returncode = process.wait()
        except Exception:
            logger.exception("Error while watching %s", game.name)
            return

        with self._lock:
            started_at = self._started_at
            still_current = self._process is process
            self._process = None
            self._game = None
            self._started_at = 0.0

        hours = (time.monotonic() - started_at) / 3600.0 if still_current and started_at else 0.0
        logger.info("Exited %s (rc %s, %.1f min)", game.name, returncode, hours * 60)
        if self.on_exit is not None:
            try:
                self.on_exit(game, hours, returncode)
            except Exception:
                logger.exception("on_exit handler failed for %s", game.name)