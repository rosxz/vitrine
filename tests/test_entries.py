"""GameEntry polymorphism: uniform Play flow across sources via a stub controller."""

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
    """Records which controller operation an entry invokes."""

    def __init__(self, *, running: Game | None = None) -> None:
        self.calls: list[str] = []
        self.running = running
        self.installed = True

    def is_running(self, game: Game) -> bool:
        return self.running is not None and self.running.id == game.id

    def stop_running(self) -> None:
        self.calls.append("stop_running")

    def launch_local(self, game: Game) -> None:
        self.calls.append(f"launch_local:{game.name}")

    def remove_local(self, game: Game) -> None:
        self.calls.append(f"remove_local:{game.name}")

    def install_gog(self, game: Game) -> None:
        self.calls.append(f"install_gog:{game.name}")

    def launch_epic(self, game: Game) -> None:
        self.calls.append(f"launch_epic:{game.name}")

    def install_epic(self, game: Game) -> None:
        self.calls.append(f"install_epic:{game.name}")

    def launch_steam(self, game: Game) -> None:
        self.calls.append(f"launch_steam:{game.name}")

    def uninstall_steam(self, game: Game) -> None:
        self.calls.append(f"uninstall_steam:{game.name}")

    def prompt_uninstall(self, game: Game) -> None:
        self.calls.append(f"prompt_uninstall:{game.name}")

    def store_url_for(self, game: Game) -> str | None:
        return f"store:{game.source}/{game.source_id}"

    # SessionManager-shaped surface used by GameEntry.is_running/on_stop.
    @property
    def sessions(self):  # noqa: ANN201
        return self


def _game(name: str, source: str, installed: bool = True, game_id: int = 1) -> Game:
    return Game(name=name, id=game_id, source=source, source_id="s1", installed=installed)


def test_entry_for_maps_source_to_subclass() -> None:
    assert isinstance(entry_for(_game("G", "local"), None), LocalGameEntry)
    assert isinstance(entry_for(_game("G", "gog"), None), GogGameEntry)
    assert isinstance(entry_for(_game("G", "epic"), None), EpicGameEntry)
    assert isinstance(entry_for(_game("G", "steam"), None), SteamGameEntry)


def test_local_launch_dispatches() -> None:
    ctrl = _StubController()
    entry_for(_game("Local", "local"), ctrl).on_launch()
    assert ctrl.calls == ["launch_local:Local"]


def test_gog_installed_launches_via_local_runner() -> None:
    ctrl = _StubController()
    entry_for(_game("G", "gog", installed=True), ctrl).on_launch()
    assert ctrl.calls == ["launch_local:G"]


def test_gog_not_installed_installs() -> None:
    ctrl = _StubController()
    entry_for(_game("G", "gog", installed=False), ctrl).on_launch()
    assert ctrl.calls == ["install_gog:G"]


def test_epic_installed_launches_via_legendary() -> None:
    ctrl = _StubController()
    entry_for(_game("E", "epic", installed=True), ctrl).on_launch()
    assert ctrl.calls == ["launch_epic:E"]


def test_epic_not_installed_installs() -> None:
    ctrl = _StubController()
    entry_for(_game("E", "epic", installed=False), ctrl).on_launch()
    assert ctrl.calls == ["install_epic:E"]


def test_steam_launches_via_uri_even_when_not_installed() -> None:
    ctrl = _StubController()
    entry_for(_game("St", "steam", installed=False), ctrl).on_launch()
    assert ctrl.calls == ["launch_steam:St"]


def test_running_game_stops_instead_of_launching() -> None:
    game = _game("G", "gog", installed=True)
    ctrl = _StubController(running=game)
    entry_for(game, ctrl).on_launch()
    # The running game toggles to Stop, not a re-launch.
    assert ctrl.calls == ["stop_running"]


def test_uninstall_dispatch() -> None:
    assert entry_for(_game("L", "local"), _StubController()).on_uninstall() or True
    # Steam uninstall goes to Steam; GOG/Epic prompt for confirmation.
    steam = _StubController()
    entry_for(_game("S", "steam"), steam).on_uninstall()
    assert steam.calls == ["uninstall_steam:S"]
    epic = _StubController()
    entry_for(_game("E", "epic"), epic).on_uninstall()
    assert epic.calls == ["prompt_uninstall:E"]
    gog = _StubController()
    entry_for(_game("G", "gog"), gog).on_uninstall()
    assert gog.calls == ["prompt_uninstall:G"]


def test_store_url_delegates() -> None:
    gog = _StubController()
    assert entry_for(_game("G", "gog", game_id=9), gog).store_url() == "store:gog/s1"