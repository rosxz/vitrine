"""Per-game behavioural polymorphism (GUI-free).

Every game — whether it came from the local library, Steam, GOG or Epic — is
wrapped in a :class:`GameEntry` whose shared ``on_launch()`` skeleton owns the
uniform *Window → Game → Play → launch* flow. Each subclass contains the real
per-source strategy: how to build the launch command/URI, whether an owned-but-
not-installed title can be installed here, and its store URL.

A ``GameEntry`` builds commands, URIs and envs itself (pure, GUI-free) and
delegates only the *execution* to a duck-typed ``controller`` (the window),
which provides generic primitives like "run this command under a log window",
"start a tracked download", "toast a message", or "open a URI".
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence

from vitrine.services.library import Game

logger = logging.getLogger(__name__)


class GameEntry(ABC):
    """Strategy for one game's launch/install/store behaviour, per source."""

    source_id: str = "local"

    def __init__(self, game: Game, controller) -> None:
        self.game = game
        self.controller = controller

    # -- shared flow -----------------------------------------------------------

    def on_launch(self) -> None:
        """Uniform *Play* handling: stop-if-running, else install, else launch."""
        if self.is_running():
            self.on_stop()
            return
        if not self.is_installed() and self.can_install():
            self.on_install()
            return
        self.launch()

    def is_running(self) -> bool:
        """Whether this exact game is the one currently running."""
        running = getattr(self.controller, "is_game_running", None)
        if callable(running):
            return bool(running(self.game))
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

    def on_install_finished(self, returncode: int, output: Sequence[str] = ()) -> bool:
        """Post-install work after a download job ends with ``returncode``.

        ``output`` is the job's accumulated log lines. Store subclasses override
        this to reconcile their installed state (e.g. Epic re-syncs legendary,
        GOG resolves the executable from its depot). Returns True unless the
        install should be considered failed.
        """
        return True

    def on_stop(self) -> None:
        """Stop the running session for this game (default: local runner)."""
        sessions = getattr(self.controller, "sessions", None)
        if sessions is not None and hasattr(sessions, "stop_running"):
            sessions.stop_running()
            return
        runtime = getattr(self.controller, "runtime", None)
        if runtime is not None:
            runtime.stop()

    def on_uninstall(self) -> None:
        raise NotImplementedError

    def install_dir(self) -> str | None:
        """The directory where this game's files live (``None`` if unknown)."""
        import os

        for candidate in (self.game.executable, self.game.working_dir):
            if not candidate:
                continue
            directory = (
                candidate if os.path.isdir(candidate) else os.path.dirname(candidate)
            )
            if directory and os.path.isdir(directory):
                return directory
        return None

    def uninstall(self, remove_prefix: bool = False) -> None:
        """Remove this game's files (+ prefix) and revert to not-installed."""
        import shutil

        install_dir = self.install_dir()
        if install_dir:
            try:
                shutil.rmtree(install_dir, ignore_errors=True)
            except OSError:  # pragma: no cover - best effort
                pass

        if remove_prefix:
            from vitrine.services.launch import wine_prefix_for

            prefix = str(wine_prefix_for(self.game))
            if os.path.isdir(prefix):
                try:
                    shutil.rmtree(prefix, ignore_errors=True)
                except OSError:  # pragma: no cover - best effort
                    pass

        # Keep the library entry, but revert it to 'available, not installed'.
        self.game.installed = False
        self.game.executable = None
        if self.game.id is not None:
            self.controller.library.update(self.game) if hasattr(self.controller, "library") else None

    def store_url(self) -> str | None:
        return None

    def _prompt_uninstall(self) -> None:
        """Default store uninstall: ask the controller to confirm then remove."""
        prompter = getattr(self.controller, "prompt_uninstall", None)
        if callable(prompter):
            prompter(self.game)
            return
        raise NotImplementedError("controller has no prompt_uninstall")

    def record_exit(self, hours: float, returncode: int | None, library) -> None:
        """Record playtime for a finished session.

        The default policy accumulates wall-clock hours locally (used by local,
        GOG and Epic games, which have no authoritative server value). Steam
        overrides this to read the value Steam itself maintains.
        """
        if hours > 0:
            library.record_playtime(self.game, hours)

    # -- controller convenience -------------------------------------------------

    def _toast(self, title: str) -> None:
        toast = getattr(self.controller, "toast", None)
        if callable(toast):
            toast(title)

    def _log_window(self, title: str):
        """Open (or return None) a debug log window per the debug-log setting.

        The controller owns the setting check and GTK window; this returns the
        log sink (or None) the launch can feed lines into.
        """
        opener = getattr(self.controller, "log_window", None)
        return opener(title) if callable(opener) else None

    def _open_log(self, log, line: str) -> None:
        if log is not None and hasattr(log, "append_line"):
            log.append_line(line)

    def _marshal(self, fn) -> None:
        marshal = getattr(self.controller, "marshal", None)
        if callable(marshal):
            marshal(fn)

    def _library(self):
        return getattr(self.controller, "library", None)


# ---------------------------------------------------------------------------
# Local games
# ---------------------------------------------------------------------------


class LocalGameEntry(GameEntry):
    """Hand-added games: installed locally, launched via the generic runner."""

    source_id = "local"

    def can_install(self) -> bool:
        return False

    def launch(self) -> None:
        runner = getattr(self.controller, "launch_local", None)
        if callable(runner):
            runner(self.game)
            return
        raise NotImplementedError("controller has no launch_local")

    def on_uninstall(self) -> None:
        remover = getattr(self.controller, "remove_local", None)
        if callable(remover):
            remover(self.game)
            return
        self._library().remove(self.game.id) if self.game.id is not None else None


# ---------------------------------------------------------------------------
# GOG games
# ---------------------------------------------------------------------------


class GogGameEntry(GameEntry):
    """GOG games: installed ones run via the generic runner; installs use gogdl."""

    source_id = "gog"

    def can_install(self) -> bool:
        return True

    def launch(self) -> None:
        runner = getattr(self.controller, "launch_local", None)
        if callable(runner):
            runner(self.game)
            return
        raise NotImplementedError("controller has no launch_local")

    def on_install(self) -> None:
        self._install_gog()

    def on_install_finished(self, returncode: int, output: Sequence[str] = ()) -> bool:
        finisher = getattr(self.controller, "finish_gog_install", None)
        if callable(finisher):
            finisher(self.game, output)
        return True

    def _install_gog(self) -> None:
        from vitrine.infra.util import slugify
        from vitrine.sources.gog import gogdl
        from vitrine.sources.gog_source import GogSource
        from vitrine.sources.steam_source import SteamAuthError  # noqa: F401

        game_id = self.game.source_id or ""
        if not game_id:
            self._toast(f"No GOG id for {self.game.name}")
            return
        if not gogdl.is_installed():
            self._toast("gogdl is required to install GOG games. Install 'gogdl' first.")
            return
        library = self._library()
        source = GogSource(library)
        if not source.is_authenticated():
            self._toast("Sign in to GOG first (cog → GOG)")
            return
        if self.controller.installing(self.game):
            self._toast(f"{self.game.name} is already downloading")
            return
        try:
            source.ensure_fresh_token()
        except Exception as exc:  # noqa: BLE001
            self._toast(f"GOG session expired — sign in again ({exc})")
            return

        store = source.login_token_store()
        from vitrine.infra import paths

        try:
            auth_path = str(paths.cache_dir() / "gogdl-auth.json")
            gogdl.write_auth_config_now(store, auth_path)
            install_path = gogdl.install_dir(self.game.slug or slugify(self.game.name))
            if gogdl.has_manifest(game_id):
                install_directory = gogdl.manifest_data(game_id).get("installDirectory")
                repair_path = os.path.join(install_path, str(install_directory)) if install_directory else install_path
                os.makedirs(repair_path, exist_ok=True)
                command = gogdl.repair_command(game_id, repair_path, auth_path)
            else:
                command = gogdl.download_command(game_id, install_path, auth_path)
        except Exception as exc:  # noqa: BLE001
            self._toast(f"Could not start GOG install for {self.game.name}: {exc}")
            return
        os.makedirs(install_path, exist_ok=True)
        self.game.config["gog_install_dir"] = install_path
        self.game.config["gog_id"] = game_id
        library.update(self.game) if self.game.id is not None else None
        self.controller.start_install_command(self.game, command, timeout=self.controller.gogdl_timeout())
        self._toast(f"Downloading {self.game.name}…")

    def store_url(self) -> str | None:
        appid = self.game.source_id or ""
        if not appid:
            return None
        return f"https://www.gog.com/en/game/{self.game.catalog_slug or appid}"

    def install_dir(self) -> str | None:
        # GOG installs land in the gogdl depot dir recorded at install time.
        configured = (self.game.config or {}).get("gog_install_dir")
        if configured:
            return str(configured)
        return super().install_dir()

    def on_uninstall(self) -> None:
        self._prompt_uninstall()


# ---------------------------------------------------------------------------
# Epic games
# ---------------------------------------------------------------------------


class EpicGameEntry(GameEntry):
    """Epic games: install/launch via legendary (storeless client)."""

    source_id = "epic"

    def can_install(self) -> bool:
        return True

    def on_stop(self) -> None:
        """Force-stop an Epic game: kill the tracked legendary job/proc."""
        stopper = getattr(self.controller, "stop_epic", None)
        if callable(stopper):
            stopper(self.game)
            return
        super().on_stop()

    def on_install_finished(self, returncode: int, output: Sequence[str] = ()) -> bool:
        # Re-sync installed state so legendary's (now-installed) games mark the
        # library rows as installed and route to Launch instead of Install.
        from vitrine.sources.epic_source import EpicSource

        EpicSource(self.controller.library).sync_installed()
        return True

    def install_dir(self) -> str | None:
        from vitrine.sources.epic import legendary as lg

        app = self.game.source_id or ""
        if app and lg.is_installed():
            try:
                exe = lg.installed_executable(app)
            except Exception:  # noqa: BLE001
                exe = None
            if exe:
                return os.path.dirname(exe)
        return super().install_dir()

    def uninstall(self, remove_prefix: bool = False) -> None:
        from vitrine.sources.epic import legendary as lg

        app = self.game.source_id or ""
        if app and lg.is_installed():
            # Legendary tracks its own installs; let it remove the files so it
            # no longer reports the app as installed.
            try:
                lg.uninstall(app)
            except Exception:  # noqa: BLE001
                self._toast(f"Could not uninstall {self.game.name}")
                return
        super().uninstall(remove_prefix)

    def launch(self) -> None:
        self._launch_epic()

    def _launch_epic(self) -> None:
        from vitrine.infra.wine import umu
        from vitrine.services.launch import _proton_dist_dir, install_d3d_extras, wine_prefix_for
        from vitrine.services.runners import has_x11_driver, load_runners_store, resolve_game_runner
        from vitrine.sources.epic import legendary as lg

        app = self.game.source_id or ""
        if not app:
            self._toast(f"No Epic app id for {self.game.name}")
            return
        if not lg.is_installed():
            self._toast("Legendary is required to run Epic games. Install 'legendary' first.")
            return
        if self.controller.installing(self.game):
            self._toast(f"{self.game.name} is already launching")
            return

        library = self._library()
        config = self.game.merged_config(library.global_config())
        store = load_runners_store(library)
        runner, wine_bin = resolve_game_runner(self.game, config, store, library=library)
        is_proton = bool(runner and runner.is_proton)
        wine_prefix = str(wine_prefix_for(self.game))

        if not has_x11_driver(wine_bin) and os.environ.get("WAYLAND_DISPLAY"):
            self._toast(
                f"{self.game.name}: the selected wine has no X11 driver (Wayland-only). "
                "Pick a Proton runner from the per-game settings."
            )
            return

        if is_proton:
            try:
                umu.umu_binary()
            except umu.UmuError as exc:
                self._toast(str(exc))
                return
            from vitrine.infra.prefix import stop_wineserver

            stop_wineserver(wine_bin, wine_prefix, steam_run=True)
            exe = lg.installed_executable(app)
            if not exe:
                self._toast(f"Could not find the installed executable for {self.game.name}")
                return
            command = umu.umu_command(exe)
            env = umu.umu_env(
                wine_prefix,
                proton_path=_proton_dist_dir(wine_bin)
                or os.path.dirname(os.path.dirname(os.path.expanduser(wine_bin))),
                game_id=app,
                install_path=os.path.dirname(exe),
            )
            # Do NOT apply the Nix driver env here (breaks pressure-vessel);
            # only carry the GL driver paths for legacy wined3d titles.
            from vitrine.infra.gpu import discover as _gpu_discover

            _gpu = _gpu_discover()
            if _gpu.dri_dir:
                env.setdefault("LIBGL_DRIVERS_PATH", _gpu.dri_dir)
                env.setdefault("MESA_DRIVER_PATH", _gpu.dri_dir)
            # Do NOT install d3d_extras here: umu/Proton seeds the prefix itself
            # (copy_pfx) and pre-writing DX runtime DLLs as real files into the
            # target prefix makes Proton's os.symlink fail with FileExistsError
            # (Proton ships its own d3dcompiler/d3dx). Instead remove any stale
            # d3d_extras we may have left from a previous run.
            from vitrine.infra.wine import d3d_extras

            d3d_extras.remove_from_prefix(wine_prefix)
            if not config.get("dxvk", True):
                off = "d3d10core=n;d3d11=n;dxgi=n"
                env.setdefault("WINEDLLOVERRIDES", "")
                env["WINEDLLOVERRIDES"] = (env["WINEDLLOVERRIDES"] + ";" if env["WINEDLLOVERRIDES"] else "") + off
            from vitrine.services.launch import apply_performance_env

            apply_performance_env(env, config)
            for key, value in (config.get("env") or {}).items():
                if key:
                    env[str(key)] = str(value)
            if config.get("locale"):
                env["LANG"] = str(config["locale"])
                env["LC_ALL"] = str(config["locale"])
            from vitrine.services.launch import gamescope_wrap

            if config.get("gamescope", False):
                command = gamescope_wrap(config, command)
            self.controller.run_owned_launch(
                self.game, command, env, os.path.dirname(exe),
                proton=True, exe=exe, wine_bin=wine_bin, wine_prefix=wine_prefix,
            )
        else:
            from vitrine.infra.prefix import prepare_prefix

            try:
                prepare_prefix(wine_bin, wine_prefix, steam_run=False)
            except ValueError as exc:
                self._toast(str(exc))
                return
            env = dict(os.environ)
            env["WINEARCH"] = "win64"
            env["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d"
            from vitrine.infra.gpu import driver_env

            env = driver_env(env)
            d3d_overrides = install_d3d_extras(wine_prefix)
            if d3d_overrides:
                env["WINEDLLOVERRIDES"] += ";" + d3d_overrides
            command = [lg.legendary_binary(), *lg.launch_command(app, wine_bin=wine_bin, wine_prefix=wine_prefix)]
            self.controller.run_owned_launch(self.game, command, env, None, proton=False)

    def on_install(self) -> None:
        self._install_epic()

    def _install_epic(self) -> None:
        from vitrine.sources.epic import legendary as lg

        app = self.game.source_id or ""
        if not app:
            self._toast(f"No Epic app id for {self.game.name}")
            return
        if not lg.is_installed():
            self._toast("Legendary is required to install Epic games. Install 'legendary' first.")
            return
        if self.controller.installing(self.game):
            self._toast(f"{self.game.name} is already downloading")
            return
        if not lg.is_authenticated():
            self._toast(f"{self.game.name}: legendary is not signed in to Epic. Sign in via cog → Epic first.")
            return
        self.game.executable = lg.installed_executable(app) or self.game.executable
        command = [lg.legendary_binary(), *lg.install_command(app)]
        self.controller.start_install_command(self.game, command)

    def store_url(self) -> str | None:
        appid = self.game.source_id or ""
        if not appid:
            return None
        return f"https://store.epicgames.com/p/{self.game.catalog_slug or appid}"

    def on_uninstall(self) -> None:
        self._prompt_uninstall()


# ---------------------------------------------------------------------------
# Steam games
# ---------------------------------------------------------------------------


class SteamGameEntry(GameEntry):
    """Steam games: handed off to Steam itself (``steam://rungameid``)."""

    source_id = "steam"

    def is_installed(self) -> bool:
        # Steam games can always be launched: Steam prompts to install owned
        # titles on first run, so we never block on a local-installed flag.
        return True

    def can_install(self) -> bool:
        return False

    def on_stop(self) -> None:
        """Force-stop a Steam game: SIGTERM, then SIGKILL if it ignores it."""
        stopper = getattr(self.controller, "stop_steam", None)
        if callable(stopper):
            stopper(self.game)
            return
        super().on_stop()

    def launch(self) -> None:
        launcher = getattr(self.controller, "launch_steam", None)
        if callable(launcher):
            launcher(self.game)
            return
        raise NotImplementedError("controller has no launch_steam")

    def on_install(self) -> None:
        # Steam owns installs; tapping an uninstalled Steam game just launches it.
        self.launch()

    def on_uninstall(self) -> None:
        uninstaller = getattr(self.controller, "uninstall_steam", None)
        if callable(uninstaller):
            uninstaller(self.game)
            return
        raise NotImplementedError("controller has no uninstall_steam")

    def store_url(self) -> str | None:
        appid = self.game.source_id or ""
        if not appid:
            return None
        return f"https://store.steampowered.com/app/{appid}"

    def record_exit(self, hours: float, returncode: int | None, library) -> None:
        # Steam is the authoritative owner of its playtime. We don't accumulate
        # wall-clock hours locally; read the value Steam wrote (manifest / Web
        # API) after the session ends, via the source, and write it back.
        from vitrine.sources.steam_source import SteamSource

        source = SteamSource(library)

        def _worker() -> None:
            fresh_hours, lastplayed = source.refresh_playtime(self.game)
            if fresh_hours is None:
                return  # not installed / no manifest; nothing authoritative.
            library.set_authoritative_playtime(self.game, fresh_hours, lastplayed)
            self.controller.apply_game_update(self.game, "Updated playtime")

        self.controller.run_async(_worker)

_ENTRIES: dict[str, type[GameEntry]] = {}


def register_entry(entry_cls: type[GameEntry]) -> type[GameEntry]:
    _ENTRIES[entry_cls.source_id] = entry_cls
    return entry_cls


def entry_for(game: Game, controller) -> GameEntry:
    """Return the :class:`GameEntry` handling ``game``, given a controller."""
    cls = _ENTRIES.get(game.source or "local", LocalGameEntry)
    return cls(game, controller)


for _cls in (LocalGameEntry, GogGameEntry, EpicGameEntry, SteamGameEntry):
    register_entry(_cls)