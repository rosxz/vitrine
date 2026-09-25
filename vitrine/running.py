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

    def start(
        self,
        game: Game,
        config: dict,
        runners_store: dict[str, str] | None = None,
        *,
        log: Callable[[str], None] | None = None,
        on_plan: Callable[[launch.LaunchPlan], None] | None = None,
    ) -> None:
        """Launch ``game`` under ``config`` and begin watching it.

        ``runners_store`` maps runner ids to their wine-binary paths so the
        selected runner can be resolved. ``log``, when given, receives each line
        of the game's stdout/stderr (used by the debug log window). Raises
        :class:`GameAlreadyRunning` if another game is still running.
        """
        with self._lock:
            if self._process is not None:
                name = self._game.name if self._game else "another game"
                raise GameAlreadyRunning(f"{name} is still running")

            plan = launch.build_launch_plan(game, config, runners_store)
            if on_plan is not None:
                on_plan(plan)
            if game.runner not in (None, "", "linux", "native"):
                wine_binary = launch.resolve_runner(
                    config.get("runner"), runners_store, config.get("wine_binary")
                )
                from .prefix import prepare_prefix

                prepare_prefix(
                    wine_binary,
                    plan.prefix or "",
                    steam_run=launch._is_proton_path(wine_binary),
                )
            process = launch.launch(plan, capture=log is not None)
            self._process = process
            self._game = game
            self._started_at = time.monotonic()

        logger.info("Started %s (pid %s)", game.name, process.pid)
        if log is not None and process.stdout is not None:
            threading.Thread(target=self._stream, args=(process, game, log), daemon=True, name="game-stream").start()
        if self.on_start is not None:
            try:
                self.on_start(game)
            except Exception:
                logger.exception("on_start handler failed for %s", game.name)

        threading.Thread(target=self._watch, args=(process, game), daemon=True, name="game-watch").start()

    def _stream(self, process: subprocess.Popen, game: Game, log: Callable[[str], None]) -> None:
        """Forward the game's piped output to ``log`` (e.g. the debug log window)."""
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    log(line.rstrip("\n"))
        except Exception:
            logger.exception("Error streaming output for %s", game.name)

    def stop(self) -> None:
        """Terminate the running game's whole process tree.

        Uses the /proc tree teardown rather than a bare SIGTERM: wrappers like
        gamescope can ignore a lone SIGTERM and leave an invisible window up.
        """
        from . import procwatch

        with self._lock:
            process = self._process
        if process is not None:
            logger.info("Stopping %s", self._game.name if self._game else "game")
            procwatch.terminate_tree(process.pid)
            import time

            time.sleep(1.0)
            if process.poll() is None:
                procwatch.kill_tree(process.pid)

    def _watch(self, process: subprocess.Popen, game: Game) -> None:
        try:
            returncode = self._wait_for_exit(process, executable=game.executable)
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

    @staticmethod
    def _wait_for_exit(
        process: subprocess.Popen,
        *,
        interval: float = 2.0,
        linger_polls: int = 3,
        executable: str | None = None,
    ) -> int:
        """Wait for the game to end, tearing down a lingering wrapper (gamescope).

        Most games exit at the same time as the process we spawned. But a wrapper
        like gamescope can outlive its child: the game window closes, the game
        process is gone, yet gamescope stays up with an invisible window and
        ``wait()`` never returns.

        For wrapped games we poll the process tree. We only tear the wrapper down
        once a *real* (non-wrapper, non-Wine-internal) descendant has been seen
        at least once (the game actually started) and then stays absent for
        ``linger_polls`` polls -- so we never kill a game that is still coming
        up or running. Plain (unwrapped) games block on ``wait()`` like always.
        """
        from . import procwatch

        if not procwatch.is_wrapper(process.pid):
            return process.wait()

        seen_game = False
        missing = 0
        while process.poll() is None:
            time.sleep(interval)
            game_present = (
                procwatch.game_present_in_tree(process.pid, executable)
                if executable is not None
                else procwatch.game_present_in_tree(process.pid)
            )
            if game_present:
                seen_game = True
                missing = 0
            elif seen_game:
                missing += 1
                if missing >= linger_polls:
                    logger.info(
                        "Game exited but its wrapper (pid %s) lingers; tearing it down",
                        process.pid,
                    )
                    procwatch.terminate_tree(process.pid)
                    time.sleep(1.0)
                    if process.poll() is None:
                        procwatch.kill_tree(process.pid)
                    break
        return process.wait()