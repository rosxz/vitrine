"""/proc process-tree helpers: wrapper/Wine-internal detection, tree teardown."""

from __future__ import annotations

import pytest

from vitrine import procwatch

# 1 (gamescope) -> 2 (gamescopereaper) -> 3 (wine) -> 4 (wineserver)
TREE: dict[int, list[int]] = {1: [2], 2: [3], 3: [4], 4: []}
ARGVS: dict[int, list[str]] = {
    1: ["gamescope", "-f", "--", "/usr/bin/wine", "/g/HuniePop.exe"],
    2: ["gamescopereaper", "--", "/usr/bin/wine", "/g/HuniePop.exe"],
    3: ["/usr/bin/wine", "/g/HuniePop.exe"],
    4: ["/usr/bin/wineserver"],
}


@pytest.fixture(autouse=True)
def _procs(monkeypatch: pytest.MonkeyPatch) -> None:
    TREE.clear()
    TREE.update({1: [2], 2: [3], 3: [4], 4: []})
    ARGVS.clear()
    ARGVS.update({
        1: ["gamescope", "-f", "--", "/usr/bin/wine", "/g/HuniePop.exe"],
        2: ["gamescopereaper", "--", "/usr/bin/wine", "/g/HuniePop.exe"],
        3: ["/usr/bin/wine", "/g/HuniePop.exe"],
        4: ["/usr/bin/wineserver"],
    })
    monkeypatch.setattr(procwatch, "children", lambda pid: TREE.get(pid, []))
    monkeypatch.setattr(procwatch, "proc_argv", lambda pid: ARGVS.get(pid, []))


def test_is_wrapper() -> None:
    assert procwatch.is_wrapper(1)   # gamescope
    assert procwatch.is_wrapper(2)   # gamescopereaper
    assert not procwatch.is_wrapper(3)  # wine is the real runner
    assert not procwatch.is_wrapper(4)  # wineserver

    ARGVS[6] = ["python3", "/opt/.umu-run-wrapped", "/games/Game.exe"]
    assert procwatch.is_wrapper(6)


def test_is_aux_classification() -> None:
    assert procwatch.is_aux(1)  # gamescope
    assert procwatch.is_aux(2)  # gamescopereaper
    assert not procwatch.is_aux(3)  # wine hosts the game, not aux
    assert procwatch.is_aux(4)  # wineserver lingers

    # A real game process is never aux.
    ARGVS[5] = ["HuniePop.exe"]
    assert not procwatch.is_aux(5)


def test_game_present_reflects_real_child() -> None:
    # While the wine child (3) is alive, there is a real non-aux descendant.
    assert procwatch.game_present_in_tree(1)

    # Game quits: only gamescopereaper (+ wineserver) remain -> not present.
    TREE[2] = []
    ARGVS.pop(3)
    assert not procwatch.game_present_in_tree(1)


def test_descendants_order() -> None:
    found = procwatch.descendants(1)
    assert set(found) == {2, 3, 4}


def test_terminate_tree_children_first(monkeypatch: pytest.MonkeyPatch) -> None:
    kills: list[int] = []
    monkeypatch.setattr(procwatch.os, "kill", lambda pid, _sig: kills.append(pid))
    procwatch.terminate_tree(1)
    assert kills[-1] == 1  # parent last
    assert set(kills) == {1, 2, 3, 4}