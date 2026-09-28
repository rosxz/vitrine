"""Per-game behavioural polymorphism (GUI-free).

Every game — whether it came from the local library, Steam, GOG or Epic — is
wrapped in a :class:`GameEntry` whose shared ``on_launch()`` skeleton owns the
uniform *Window → Game → Play → launch* flow. Source differences (Steam hands
off to a ``steam://`` URI, Epic goes through legendary, GOG/local use the local
runner) are expressed as overrides on subclasses, so callers no longer switch
on ``game.source``.

A ``GameEntry`` is intentionally thin: it holds the library entry plus a duck-
typed ``controller`` (the window in the desktop app) that provides the concrete,
GTK-coupled side-effects (toasts, download state, process supervision). Keeping
``controller`` behind a small protocol keeps this module GUI-free and testable
with a stub.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .library import Game


class GameEntry(ABC):
    """Base for a game's launch/install/uninstall behaviour, specialised per kind.

    Subclasses override the protected hooks; the shared flow lives in
    :meth:`on_launch`. ``controller`` is duck-typed and must provide whatever a
    subclass needs (``sessions``, ``toasts``, ``library``, ``runtime``, …).
    """

    def __init__(self, game: Game, controller) -> None:
        self.game = game
        self.controller = controller

    # -- shared flow -----------------------------------------------------------

    def on_launch(self) -> None:
        """Uniform *Play* handling: stop-if-running, else install, else launch.

        This is the single entry point the UI calls for every source — clicking
        the Play button on a tile or in the hero bar ends here.
        """
        if self.is_running():
            self.on_stop()
            return
        if not self.is_installed() and self.can_install():
            self.on_install()
            return
        self.launch()

    def is_running(self) -> bool:
        """Whether this exact game is the one currently running.

        Prefers a controller-provided identity check (the window compares by
        library id, then source/source_id, then name); falls back to the
        SessionManager's id-based check.
        """
        controller_running = getattr(self.controller, "is_game_running", None)
        if callable(controller_running):
            return bool(controller_running(self.game))
        sessions = getattr(self.controller, "sessions", None)
        if sessions is not None and hasattr(sessions, "is_running"):
            return bool(sessions.is_running(self.game))
        return False

    def is_installed(self) -> bool:
        return bool(self.game.installed)

    def can_install(self) -> bool:
        """Whether an owned-but-not-installed entry can be installed here."""
        return False

    # -- hooks (per-source) ----------------------------------------------------

    @abstractmethod
    def launch(self) -> None:
        """Launch the installed game."""

    def on_install(self) -> None:
        raise NotImplementedError

    def on_stop(self) -> None:
        """Stop the running session for this game."""
        controller_stop = getattr(self.controller, "stop_game", None)
        if callable(controller_stop):
            controller_stop()
            return
        sessions = getattr(self.controller, "sessions", None)
        if sessions is not None and hasattr(sessions, "stop_running"):
            sessions.stop_running()
            return
        runtime = getattr(self.controller, "runtime", None)
        if runtime is not None:
            runtime.stop()

    def on_uninstall(self) -> None:
        raise NotImplementedError

    def store_url(self) -> str | None:
        return None

    def install_dir(self) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Per-source specialisations. Each chooses the controller operation it needs;
# the heavy, GTK-coupled side-effects live on the controller (the window).
# ---------------------------------------------------------------------------


class LocalGameEntry(GameEntry):
    """Hand-added games: installed locally, launched via the local runner."""

    source_id = "local"

    def can_install(self) -> bool:
        return False

    def launch(self) -> None:
        self.controller.launch_local(self.game)

    def on_uninstall(self) -> None:
        self.controller.remove_local(self.game)


class GogGameEntry(GameEntry):
    """GOG games: genically native or run through the local Wine pipeline."""

    source_id = "gog"

    def can_install(self) -> bool:
        return True

    def launch(self) -> None:
        self.controller.launch_local(self.game)

    def on_install(self) -> None:
        self.controller.install_gog(self.game)

    def on_uninstall(self) -> None:
        self.controller.prompt_uninstall(self.game)

    def store_url(self) -> str | None:
        return self.controller.store_url_for(self.game)


class EpicGameEntry(GameEntry):
    """Epic games: launch/install via legendary (storeless client)."""

    source_id = "epic"

    def can_install(self) -> bool:
        return True

    def launch(self) -> None:
        self.controller.launch_epic(self.game)

    def on_install(self) -> None:
        self.controller.install_epic(self.game)

    def on_uninstall(self) -> None:
        self.controller.prompt_uninstall(self.game)

    def store_url(self) -> str | None:
        return self.controller.store_url_for(self.game)


class SteamGameEntry(GameEntry):
    """Steam games: hand-off to Steam itself (``steam://rungameid``)."""

    source_id = "steam"

    def is_installed(self) -> bool:
        # Steam games can always be launched: Steam prompts to install owned
        # titles on first run, so we never block on a local-installed flag.
        return True

    def can_install(self) -> bool:
        return False

    def launch(self) -> None:
        self.controller.launch_steam(self.game)

    def on_install(self) -> None:
        # Steam owns installs; tapping an uninstalled Steam game just launches
        # it (Steam prompts to install). No separate install step.
        self.controller.launch_steam(self.game)

    def on_uninstall(self) -> None:
        self.controller.uninstall_steam(self.game)

    def store_url(self) -> str | None:
        return self.controller.store_url_for(self.game)


_ENTRIES: dict[str, type[GameEntry]] = {}


def register_entry(entry_cls: type[GameEntry]) -> type[GameEntry]:
    _ENTRIES[entry_cls.source_id] = entry_cls
    return entry_cls


def entry_for(game: Game, controller) -> GameEntry:
    """Return the :class:`GameEntry` handling ``game``, given a controller."""
    cls = _ENTRIES.get(game.source or "local", LocalGameEntry)
    return cls(game, controller)


# Register the built-in entry kinds.
for _cls in (LocalGameEntry, GogGameEntry, EpicGameEntry, SteamGameEntry):
    register_entry(_cls)