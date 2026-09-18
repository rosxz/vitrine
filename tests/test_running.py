"""Process supervision tests."""

from __future__ import annotations

import shutil
import sys
import time

import pytest

from vitrine import db
from vitrine.library import Game, Library
from vitrine.running import GameAlreadyRunning, Runtime

SLEEP = shutil.which("sleep")
pytestmark = pytest.mark.skipif(not SLEEP, reason="a 'sleep' executable is required")


@pytest.fixture
def library() -> Library:
    conn = db.connect(":memory:")
    db.initialize(conn)
    return Library(conn)


def sleep_game(library: Library, name: str = "Sleepy") -> Game:
    return library.add(Game(name=name, runner="linux", executable=SLEEP, arguments="1", source="local"))


def test_start_and_exit_reports_playtime(library: Library) -> None:
    game = sleep_game(library)
    runtime = Runtime()
    started: list[Game] = []
    exited: list[tuple] = []

    runtime.on_start = started.append
    runtime.on_exit = lambda g, hours, rc: exited.append((g, hours, rc))

    runtime.start(game, {})
    assert game.id in {g.id for g in started}

    deadline = time.monotonic() + 5
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)

    assert len(exited) == 1, "runtime did not report the process exit"
    exited_game, hours, returncode = exited[0]
    assert exited_game.id == game.id
    assert returncode == 0
    assert hours > 0
    assert not runtime.running


def test_stop_terminates_the_process(library: Library) -> None:
    game = library.add(Game(name="Long", executable=SLEEP, arguments="30", source="local"))
    runtime = Runtime()
    exited: list[tuple] = []
    runtime.on_exit = lambda g, hours, rc: exited.append((g, hours, rc))

    runtime.start(game, {})

    deadline = time.monotonic() + 5
    while not runtime.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runtime.running

    runtime.stop()

    deadline = time.monotonic() + 5
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exited, "process did not exit after stop"
    _, hours, returncode = exited[0]
    assert returncode != 0
    assert hours > 0


def test_cannot_start_while_running(library: Library) -> None:
    game = sleep_game(library)
    runtime = Runtime()

    runtime.start(game, {})

    deadline = time.monotonic() + 5
    while not runtime.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runtime.running

    other = library.add(Game(name="Other", executable=SLEEP, arguments="1", source="local"))
    with pytest.raises(GameAlreadyRunning):
        runtime.start(other, {})

    runtime.stop()


def test_missing_runner_raises_oserror(library: Library) -> None:
    game = library.add(Game(name="Bogus", executable="/does/not/exist.exe", source="local"))
    runtime = Runtime()

    with pytest.raises(OSError):
        runtime.start(game, {})
    assert not runtime.running


def test_no_side_effects_until_started() -> None:
    runtime = Runtime()

    assert not runtime.running
    assert runtime.running_game is None
    runtime.stop()  # must be a no-op


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))