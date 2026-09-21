"""Tests for runner (Wine/Proton) discovery, resolution, and management."""

from __future__ import annotations

from pathlib import Path

import pytest

from vitrine.runners import (
    DEFAULT_PROTON_SETTING,
    install_runner,
    list_runners,
    load_runners_store,
    remove_runner,
    resolve_runner,
    runner_installed,
    save_runners_store,
)


@pytest.fixture
def runners_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Lutris-style runners dir with two Wine builds."""
    import vitrine.runners as runners

    for name in ("wine-ge-8-26", "proton-cachyos"):
        base = tmp_path / "runners" / name
        (base / "bin").mkdir(parents=True)
        exe = base / "bin" / "wine"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setattr(runners, "RUNNER_DIRS", (str(tmp_path / "runners"),))
    return tmp_path / "runners"


def test_presets_always_listed() -> None:
    ids = [r.id for r in list_runners({})]
    assert "wine-64" in ids
    assert "wine-32" in ids
    assert "wine-ge-custom" in ids


def test_discover_on_disk(runners_dir: Path) -> None:
    ids = [r.id for r in list_runners({})]
    assert "wine-ge-8-26" in ids
    assert "proton-cachyos" in ids
    proton = next(r for r in list_runners({}) if r.id == "proton-cachyos")
    assert proton.kind == "proton"


def test_store_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.data = {}

        def setting(self, key, default=None):
            return self.data.get(key, default)

        def set_setting(self, key, value) -> None:
            self.data[key] = value

    store = FakeStore()
    save_runners_store(store, {"ge-proton": "/path/to/wine"})
    loaded = load_runners_store(store)
    assert loaded == {"ge-proton": "/path/to/wine"}


def test_resolve_default_uses_wine_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import vitrine.runners as runners

    was = runners.shutil.which
    monkeypatch.setattr(runners.shutil, "which", lambda name: "/usr/bin/wine")
    assert resolve_runner(None, {}, None).endswith("wine")
    assert resolve_runner("wine-64", {}, None).endswith("wine")
    monkeypatch.setattr(runners.shutil, "which", was)


def test_resolve_explicit_runner(runners_dir: Path) -> None:
    path = resolve_runner("wine-ge-8-26", {}, None)
    assert "wine-ge-8-26" in path
    assert path.endswith("wine") or "wine" in path


def test_install_and_remove_registration() -> None:
    store = install_runner("my-ge", "/opt/wine-ge", {})
    assert store["my-ge"] == "/opt/wine-ge"
    assert runner_installed("my-ge", store)
    store = remove_runner("my-ge", store)
    assert "my-ge" not in store
    assert not runner_installed("my-ge", store)


def test_runner_path_resolves(runners_dir: Path) -> None:
    from vitrine.runners import runner_path

    path = runner_path("wine-ge-8-26", {})
    assert path.endswith("bin/wine")
    assert runner_path("does-not-exist", {}) == ""


def test_default_proton_setting_key() -> None:
    assert DEFAULT_PROTON_SETTING == "default_runner"


def test_launch_plan_uses_resolved_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from vitrine.launch import build_launch_plan
    from vitrine.library import Game

    # Patch where launch.py actually looks it up.
    monkeypatch.setattr(
        "vitrine.launch.resolve_runner",
        lambda *a, **k: "/opt/wine-ge/bin/wine",
    )
    game = Game(name="G", executable="/bin/sh", runner="wine", config={"runner": "ge-proton-1"})
    plan = build_launch_plan(game, {"wine_binary": "wine"}, {"ge-proton-1": "/opt/wine-ge/bin/wine"})
    assert "/opt/wine-ge/bin/wine" in plan.command


def test_run_game_non_native_starts_with_wine(runners_dir: Path) -> None:
    from vitrine.launch import build_launch_plan
    from vitrine.library import Game

    game = Game(name="G", executable="/opt/game.exe", runner="wine")
    plan = build_launch_plan(game, {"wine_binary": None}, {})
    assert plan.command[0] == "wine" or "wine" in plan.command[0]
    assert "/opt/game.exe" in plan.command