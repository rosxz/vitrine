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

def test_wait_for_exit_tears_down_lingering_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a gamescope wrapper outlives the game, _wait_for_exit must kill it
    so the Playing state reverts."""
    from vitrine import procwatch
    from vitrine.running import Runtime

    calls = {"term": 0, "kill": 0, "presence": 0}
    monkeypatch.setattr(procwatch, "is_wrapper", lambda pid: True)

    def _present(_pid) -> bool:
        calls["presence"] += 1
        return calls["presence"] <= 2  # game seen present twice, then gone

    monkeypatch.setattr(procwatch, "game_present_in_tree", _present)
    monkeypatch.setattr(procwatch, "terminate_tree", lambda pid: calls.__setitem__("term", calls["term"] + 1))
    monkeypatch.setattr(procwatch, "kill_tree", lambda pid: calls.__setitem__("kill", calls["kill"] + 1))

    class _Fake:
        pid = 1

        def poll(self):
            return None  # wrapper never exits on its own

        def wait(self):
            return 143  # what we observe after a teardown

    code = Runtime._wait_for_exit(_Fake(), interval=0.01, linger_polls=1)
    assert calls["term"] >= 1
    assert calls["kill"] >= 1
    assert code == 143


def test_wait_for_exit_never_tears_down_before_game_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never kill before a real game process appeared (e.g. slow Wine boot)."""
    from vitrine import procwatch
    from vitrine.running import Runtime

    term = []
    monkeypatch.setattr(procwatch, "is_wrapper", lambda pid: True)
    monkeypatch.setattr(procwatch, "game_present_in_tree", lambda pid: False)
    monkeypatch.setattr(procwatch, "terminate_tree", lambda pid: term.append(pid))
    monkeypatch.setattr(procwatch, "kill_tree", lambda pid: term.append(pid))

    class _Fake:
        pid = 7
        left = 3

        def poll(self):
            if self.left > 0:
                self.left -= 1
                return None  # wrapper keeps running for a few polls
            return 0

        def wait(self):
            return 0

    assert Runtime._wait_for_exit(_Fake(), interval=0.01, linger_polls=1) == 0
    assert term == []  # game process never seen -> must never tear down


def test_wait_for_exit_plain_game_just_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unwrapped games (wine/native) block on wait(); never force-killed."""
    from vitrine import procwatch
    from vitrine.running import Runtime

    kills = []
    monkeypatch.setattr(procwatch, "is_wrapper", lambda pid: False)
    monkeypatch.setattr(procwatch, "terminate_tree", lambda pid: kills.append(pid))
    monkeypatch.setattr(procwatch, "kill_tree", lambda pid: kills.append(pid))

    class _Fake:
        pid = 1

        def poll(self):
            return 0

        def wait(self):
            return 0

    assert Runtime._wait_for_exit(_Fake()) == 0
    assert kills == []


def test_stop_force_kills_stubborn_process(library: Library) -> None:
    """Stop must SIGKILL a process that ignores SIGTERM (e.g. a lingering
    wrapper), so the Playing state can always be torn down."""
    import sys

    from vitrine.running import Runtime

    script = (
        "import signal, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    game = library.add(
        Game(name="Stubborn", runner="linux", executable=sys.executable,
             arguments="-c " + script, source="local")
    )
    runtime = Runtime()
    exited: list[tuple] = []
    runtime.on_exit = lambda g, hours, rc: exited.append((g, hours, rc))

    runtime.start(game, {})
    deadline = time.monotonic() + 5
    while not runtime.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runtime.running

    runtime.stop()

    deadline = time.monotonic() + 6
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exited, "stubborn process was not force-killed"
    _, _, returncode = exited[0]
    assert returncode not in (0, None)


def test_log_callback_streams_output(library: Library) -> None:
    """The debug-log path: a log callback receives the game's stdout lines."""
    import sys

    from vitrine.running import Runtime

    script = "print('hello-vitrine'); import time; time.sleep(0.2)"
    game = library.add(
        Game(name="Echo", runner="linux", executable=sys.executable,
             arguments="-c " + script, source="gog")
    )
    lines: list[str] = []
    runtime = Runtime()
    exited: list[tuple] = []
    runtime.on_exit = lambda g, hours, rc: exited.append((g, hours, rc))

    runtime.start(game, {}, log=lines.append)

    deadline = time.monotonic() + 6
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exited, "process did not exit"
    assert any("hello-vitrine" in line for line in lines), f"no streamed line: {lines}"
