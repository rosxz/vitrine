"""Steam process-free watching: /proc token detection and SIGTERM trees."""

from __future__ import annotations

import threading

import pytest

from vitrine import steamwatch


def test_appid_token_match(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = {
        100: ["reaper", "570", "/home/u/.steam/steam/steamapps/common/dota 2/game"],
        101: ["/bin/dota2", "5700", "--fullscreen"],  # "5700" != "570"
        102: ["unrelated"],
    }
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))

    assert steamwatch.steam_game_is_running("570")
    assert steamwatch.steam_game_pids("570") == [100]
    assert not steamwatch.steam_game_is_running("999")


def test_installdir_refines_match(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = {
        100: ["reaper", "570", "/home/u/Steam/steamapps/common/dota/bin/dota2"],
        101: ["reaper", "570", "/opt/somewhere/else/game"],
    }
    exes = {100: "/home/u/Steam/steamapps/common/dota/bin/dota2", 101: "/opt/somewhere/else/game"}
    cwds = {100: "/home/u/Steam/steamapps/common/dota", 101: "/opt/somewhere/else"}
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(steamwatch, "_proc_exe", lambda pid: exes.get(pid, ""))
    monkeypatch.setattr(steamwatch, "_proc_cwd", lambda pid: cwds.get(pid, ""))

    installdir = "/home/u/Steam/steamapps/common/dota"
    assert steamwatch.steam_game_pids("570", installdir) == [100]


def test_terminate_tree_children_first(monkeypatch: pytest.MonkeyPatch) -> None:
    children = {10: [20, 30], 20: [], 30: [40], 40: []}
    monkeypatch.setattr(steamwatch, "_children", lambda pid: children.get(pid, []))
    kills: list[int] = []
    monkeypatch.setattr(steamwatch.os, "kill", lambda pid, _sig: kills.append(pid))

    steamwatch.terminate_tree(10)
    assert kills[-1] == 10  # parent signalled last
    assert set(kills) == {10, 20, 30, 40}


def test_terminate_game_sends_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = {200: ["reaper", "570", "/game"]}
    children = {200: [210], 210: []}
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(steamwatch, "_children", lambda pid: children.get(pid, []))
    kills: list[int] = []
    monkeypatch.setattr(steamwatch.os, "kill", lambda pid, _sig: kills.append(pid))

    steamwatch.terminate_game("570")
    assert 200 in kills and 210 in kills


def test_watcher_fires_start_then_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"running": True}
    monkeypatch.setattr(steamwatch, "steam_game_is_running", lambda *_: state["running"])
    started = threading.Event()
    exited = threading.Event()
    watcher = steamwatch.SteamSessionWatcher(
        "570", on_start=started.set, on_exit=exited.set, interval=0.05
    )
    watcher.start()
    assert started.wait(1.0)
    state["running"] = False
    assert exited.wait(1.0)
    watcher.stop()
    assert watcher._thread is None or not watcher._thread.is_alive()

def test_steamappid_env_is_primary_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wine/Proton trees don't carry the appid in argv; the env 'SteamAppId'
    Steam sets on every spawned process is the reliable signal."""
    procs = {
        300: ["python3", "/proton", "waitforexitandrun", "/exe"],
        301: ["/bin/other"],
    }
    envs = {300: {"SteamAppId": "2084300", "SteamGameId": "2084300"}, 301: {}}
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(steamwatch, "_proc_environ", lambda pid: envs.get(pid, {}))
    monkeypatch.setattr(steamwatch, "_proc_exe", lambda pid: "")
    monkeypatch.setattr(steamwatch, "_proc_cwd", lambda pid: "")

    assert steamwatch.steam_game_pids("2084300") == [300]
    assert steamwatch.steam_game_is_running("2084300")
    assert not steamwatch.steam_game_is_running("999")


def test_wine_internal_never_counts_as_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """wineserver/services carry SteamAppId but linger after quit; the game must
    only count as running while its real process is alive."""
    procs = {
        400: ["/proton/files/bin/wineserver"],                 # lingering wineserver
        401: ["C:\\windows\\system32\\services.exe"],
        402: ["C:\\windows\\system32\\explorer.exe", "/desktop"],
        403: ["S:\\steamapps\\common\\Schism\\Schism Experimental.exe"],  # the game
    }
    exes = {
        400: "/proton/files/bin/wineserver",
        401: "/proton/files/lib/wine/i386-windows/services.exe",
        402: "/proton/files/lib/wine/x86_64-windows/explorer.exe",
        403: "/wine/prefix/drive_c/steamapps/common/Schism/Schism Experimental.exe",
    }
    envs = {pid: {"SteamAppId": "2084300"} for pid in procs}
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(steamwatch, "_proc_environ", lambda pid: envs.get(pid, {}))
    monkeypatch.setattr(steamwatch, "_proc_exe", lambda pid: exes.get(pid, ""))
    monkeypatch.setattr(steamwatch, "_proc_cwd", lambda pid: "")

    # Only Wine-internal processes (wineserver + windowing services) left
    # means the game itself has exited.
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: [400, 401, 402])
    assert not steamwatch.steam_game_is_running("2084300")

    # Once the game's real process is present again it counts as running.
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    assert steamwatch.steam_game_is_running("2084300")

    # Termination still targets the whole tree (game + services).
    assert set(steamwatch.steam_game_pids("2084300")) == {400, 401, 402, 403}


def test_terminate_kills_wineserver_also(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = {500: ["/proton/files/bin/wineserver"]}
    exes = {500: "/proton/files/bin/wineserver"}
    envs = {500: {"SteamAppId": "9"}}
    children = {500: []}
    monkeypatch.setattr(steamwatch, "_all_pids", lambda: list(procs))
    monkeypatch.setattr(steamwatch, "_proc_argv", lambda pid: procs.get(pid, []))
    monkeypatch.setattr(steamwatch, "_proc_environ", lambda pid: envs.get(pid, {}))
    monkeypatch.setattr(steamwatch, "_proc_exe", lambda pid: exes.get(pid, ""))
    monkeypatch.setattr(steamwatch, "_children", lambda pid: children.get(pid, []))
    kills: list[int] = []
    monkeypatch.setattr(steamwatch.os, "kill", lambda pid, _sig: kills.append(pid))
    steamwatch.terminate_game("9")
    assert 500 in kills
