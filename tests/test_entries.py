"""GameEntry polymorphism: uniform Play flow across sources via a stub controller.

The GOG/Epic install and launch paths build real gogdl/legendary commands, so
those engine dependencies are monkeypatched; we assert the *strategy* each entry
chooses (which controller primitive it calls and what store URL it returns), not
the downloaded CLI internals.
"""

from __future__ import annotations

from vitrine.entries import (
    EpicGameEntry,
    GogGameEntry,
    LocalGameEntry,
    SteamGameEntry,
    entry_for,
)
from vitrine.library import Game


class _StubController:
    """Records which controller operation an entry invokes, with stub services."""

    def __init__(self, *, running: Game | None = None) -> None:
        self.calls: list[str] = []
        self.running = running
        self.sessions = self

    def is_running(self, game: Game) -> bool:
        return self.running is not None and self.running.id == game.id

    def is_game_running(self, game: Game) -> bool:
        return self.running is not None and self.running.id == game.id

    def stop_game(self) -> None:
        self.calls.append("stop_game")

    def toast(self, title: str) -> None:
        self.calls.append(f"toast:{title}")

    def installing(self, game: Game) -> bool:
        return False

    def gogdl_timeout(self) -> float:
        return 30.0

    def start_install_command(self, game: Game, command: list[str], *, timeout: float | None = None) -> None:
        self.calls.append(f"start_install_command:{game.name}:{' '.join(command)}")

    def run_owned_launch(self, game: Game, command, env, cwd=None, **kwargs) -> None:
        self.calls.append(f"run_owned_launch:{game.name}:{' '.join(command[:2])}")

    def launch_local(self, game: Game) -> None:
        self.calls.append(f"launch_local:{game.name}")

    def launch_steam(self, game: Game) -> None:
        self.calls.append(f"launch_steam:{game.name}")

    def remove_local(self, game: Game) -> None:
        self.calls.append(f"remove_local:{game.name}")

    def prompt_uninstall(self, game: Game) -> None:
        self.calls.append(f"prompt_uninstall:{game.name}")

    def uninstall_steam(self, game: Game) -> None:
        self.calls.append(f"uninstall_steam:{game.name}")


class _StubLibrary:
    def global_config(self) -> dict:
        return {}

    def setting(self, key, default=None):
        return default

    def update(self, game: Game) -> None:
        pass


def _game(name: str, source: str, installed: bool = True, game_id: int = 1, source_id: str = "s1") -> Game:
    return Game(name=name, id=game_id, source=source, source_id=source_id, installed=installed)


def _stub_controller(running: Game | None = None) -> _StubController:
    ctrl = _StubController(running=running)
    ctrl.library = _StubLibrary()
    ctrl.marshal = lambda fn: fn()
    return ctrl


def test_entry_for_maps_source_to_subclass() -> None:
    assert isinstance(entry_for(_game("G", "local"), None), LocalGameEntry)
    assert isinstance(entry_for(_game("G", "gog"), None), GogGameEntry)
    assert isinstance(entry_for(_game("G", "epic"), None), EpicGameEntry)
    assert isinstance(entry_for(_game("G", "steam"), None), SteamGameEntry)


def test_local_launch_dispatches() -> None:
    ctrl = _stub_controller()
    entry_for(_game("Local", "local"), ctrl).on_launch()
    assert ctrl.calls == ["launch_local:Local"]


def test_gog_installed_launches_via_local_runner() -> None:
    ctrl = _stub_controller()
    entry_for(_game("G", "gog", installed=True), ctrl).on_launch()
    assert ctrl.calls == ["launch_local:G"]


def test_gog_not_installed_starts_install(monkeypatch) -> None:
    ctrl = _stub_controller()
    # Stub the engine-side GOG pieces so the strategy runs without a CLI.
    monkeypatch.setattr("vitrine.sources.gog_source.GogSource", lambda lib: _FakeGogSource())
    monkeypatch.setattr("vitrine.sources.gog.gogdl.is_installed", lambda: True)
    monkeypatch.setattr("vitrine.sources.gog.gogdl.has_manifest", lambda _id: False)
    monkeypatch.setattr("vitrine.sources.gog.gogdl.download_command", lambda _id, path, auth: ["gogdl", "download", _id])  # noqa: E501
    monkeypatch.setattr("vitrine.sources.gog.gogdl.install_dir", lambda slug: "/tmp/gog")
    monkeypatch.setattr("vitrine.sources.gog.gogdl.write_auth_config_now", lambda _store, _path: "/tmp/auth")
    monkeypatch.setattr("vitrine.util.slugify", lambda name: "g")
    entry_for(_game("G", "gog", installed=False), ctrl).on_launch()
    assert any(call.startswith("start_install_command:G:gogdl download") for call in ctrl.calls)


def test_epic_installed_launches_via_legendary(monkeypatch) -> None:
    ctrl = _stub_controller()
    monkeypatch.setattr("vitrine.sources.epic.legendary.is_installed", lambda: True)
    monkeypatch.setattr("vitrine.sources.epic.legendary.is_authenticated", lambda: True)
    monkeypatch.setattr("vitrine.sources.epic.legendary.installed_executable", lambda _app: "/games/E.exe")
    monkeypatch.setattr("vitrine.sources.epic.legendary.launch_command", lambda *_a, **_k: ["legendary", "launch", "E"])
    monkeypatch.setattr("vitrine.sources.epic.legendary.legendary_binary", lambda: "/bin/legendary")
    monkeypatch.setattr("vitrine.runners.has_x11_driver", lambda _w: True)
    monkeypatch.setattr("vitrine.runners.resolve_game_runner", lambda *_a, **_k: (None, "/bin/wine"))
    monkeypatch.setattr("vitrine.prefix.prepare_prefix", lambda *_a, **_k: None)
    entry_for(_game("E", "epic", installed=True), ctrl).on_launch()
    assert any(call.startswith("run_owned_launch:E:") for call in ctrl.calls)


def test_epic_not_installed_starts_install(monkeypatch) -> None:
    ctrl = _stub_controller()
    monkeypatch.setattr("vitrine.sources.epic.legendary.is_installed", lambda: True)
    monkeypatch.setattr("vitrine.sources.epic.legendary.is_authenticated", lambda: True)
    monkeypatch.setattr("vitrine.sources.epic.legendary.install_command", lambda _app: ["install", "E"])
    monkeypatch.setattr("vitrine.sources.epic.legendary.legendary_binary", lambda: "/bin/legendary")
    monkeypatch.setattr("vitrine.sources.epic.legendary.installed_executable", lambda _app: "")
    entry_for(_game("E", "epic", installed=False), ctrl).on_launch()
    assert any(call.startswith("start_install_command:E:") for call in ctrl.calls)


def test_steam_launches_via_uri_even_when_not_installed() -> None:
    ctrl = _stub_controller()
    entry_for(_game("St", "steam", installed=False), ctrl).on_launch()
    assert ctrl.calls == ["launch_steam:St"]


def test_running_game_stops_instead_of_launching() -> None:
    game = _game("G", "gog", installed=True)
    ctrl = _stub_controller(running=game)
    entry_for(game, ctrl).on_launch()
    assert ctrl.calls == ["stop_game"]


def test_uninstall_dispatch() -> None:
    steam = _stub_controller()
    entry_for(_game("S", "steam"), steam).on_uninstall()
    assert steam.calls == ["uninstall_steam:S"]
    epic = _stub_controller()
    entry_for(_game("E", "epic"), epic).on_uninstall()
    assert epic.calls == ["prompt_uninstall:E"]
    gog = _stub_controller()
    entry_for(_game("G", "gog"), gog).on_uninstall()
    assert gog.calls == ["prompt_uninstall:G"]


def test_store_url_is_per_source() -> None:
    assert entry_for(_game("G", "gog", game_id=9, source_id="abc"), _stub_controller()).store_url() == (
        "https://www.gog.com/en/game/abc"
    )
    assert entry_for(_game("E", "epic", game_id=9, source_id="app1"), _stub_controller()).store_url() == (
        "https://store.epicgames.com/p/app1"
    )
    assert entry_for(_game("S", "steam", game_id=9, source_id="570"), _stub_controller()).store_url() == (
        "https://store.steampowered.com/app/570"
    )


class _FakeGogSource:
    def is_authenticated(self) -> bool:
        return True

    def ensure_fresh_token(self) -> None:
        pass

    def login_token_store(self):
        return None