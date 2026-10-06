# Vitrine architecture

A unified game library launcher for Linux. Vitrine merges **local** installs, **Steam**
(owned + Family), **GOG** and **Epic** into one library, with custom cover/banner
artwork and per-game launcher options.

This document explains the directory layout, what each module does, and how the
important flows (launch, install, sync, login, artwork, playtime, stop) hang together.
It is written for the *post-restructure* layout (`domain/ services/ infra/ sources/ ui/`).

---

## Architectural principles

1. **Layerised imports, one-way dependencies.** The package is split into
   `domain/` → `services/` → `infra/`, with `sources/` (per-store adapters) and `ui/`
   (GTK only) at the edges. Nothing GUI-related is imported by the engine layers.
2. **GUI-free engine.** `domain/`, `services/`, `infra/` and `sources/` never import GTK.
   Only `ui/` (and `application.py`/`__main__.py`) may touch `gi.repository`.
3. **Polymorphism over source switches.** Per-source behaviour lives in `GameEntry`
   subclasses (and `Source` subclasses), not in `window.py` if/else chains. The window
   calls `entry_for(game).on_launch()` and the subclass decides what "launch" means.
4. **Threading rule.** The SQLite connection is main-thread only. Worker threads return
   results; a small `_marshal`/`GLib.idle_add` bridge hops back to the GTK loop.

---

## Directory map

```
vitrine/
  application.py   GTK `Adw.Application`; builds the DB/Library and shows the window.
  __main__.py      `python -m vitrine` entry point.
  __init__.py      APP_ID / APP_NAME / SCHEMA_VERSION.

  domain/          Pure model & per-game behaviour (no DB, no GTK, no leading subprocess).
    game.py        `Game` dataclass + `DEFAULT_CONFIG`. The library entry / launcher config.
    source.py      `Source` ABC (template-method catalogue sync), `SourceGame` DTO,
                   `SourceRegistry` + the `registry` singleton.
    entry.py       `GameEntry` ABC + `LocalGameEntry`/`GogGameEntry`/`EpicGameEntry`/
                   `SteamGameEntry`. The **strategy** for launch/install/uninstall/stop.
    session.py     `Session` interface, `RunningSession`, `SessionManager` (the single
                   running game + concurrent installs).
    runner.py      `Runner` value object (`is_proton`, `is_native`, `is_preset`).

  services/        Use-case orchestration & the library repository.
    library.py     `Library` CRUD/settings repository over sqlite (settings keys).
    launch.py      Build a `LaunchPlan` (command/env/cwd/prefix) for a game: native,
                   Wine or Proton (umu); gamescope/MangoHud/GameMode wrappers; d3d_extras.
    runners.py     Runner discovery/resolution (`resolve_runner`, `resolve_game_runner`).
    runners_source.py  Downloadable runner/Proton catalogue.
    downloads.py   `DownloadJob` + `run_download`: background process with % progress.
    sync.py        `SyncService`: refresh/reset a source via the registry.
    artwork.py     Artwork orchestration: credentials/priority loading, caching, fetch.
    artwork_providers/  IGDB / SteamGridDB / Lutris / Steam CDN adapters (`Art` DTOs).

  infra/           OS/process/network/filesystem plumbing.
    db.py          SQLite connection, schema, migrations (v0..v4).
    paths.py       XDG paths (`~/.local/share/vitrine`, `~/.cache/vitrine`, …).
    util.py        `now()`, `slugify()`, misc helpers.
    procwatch.py   Generic /proc process-tree helpers (children/descendants, terminate/kill).
    steamwatch.py  Watch / kill Steam-launched games via /proc (`SteamSessionWatcher`).
    running.py     `Runtime`: supervise ONE owned game process -> playtime on exit.
    prefix.py      Wine prefix prep/architecture (`prepare_prefix`, `recreate_prefix`).
    gpu.py         GPU driver discovery (`driver_env`, `apply_gpu_env`).
    wine/umu.py    umu-run (Proton unified launcher): command + clean env.
    wine/d3d_extras.py  DirectX 9/10/11 runtime DLLs into a prefix.

  sources/         Per-store *provider* adapters (each implements the `Source` ABC).
    base.py        Re-exports the `Source`/`SourceGame`/registry from `domain.source`.
    local.py       Local games (added by hand; nothing to fetch).
    steam_source.py / gog_source.py / epic_source.py   `Source` subclasses + helper accessors.
    auth.py        Shared `CookieJar` + `JsonCredentialStore` (Steam & GOG credentials).
    steam/{config,vdf,auth}   VDF parsing, install discovery, durable cookie/token store.
    gog/{gogdl,installer,auth}  GOG depot CLI, offline installer, OAuth store.
    epic/{legendary,auth,config}  legendary CLI wrapper, OAuth store, EGS manifests.

  ui/              GTK4 + libadwaita front end (the only GTK-importing layer).
    window.py      `VitrineWindow`: sidebar, grid, detail bar; calls `entry_for(...)`.
    library_view.py / game_detail_bar.py / game_form.py / game_dialogs.py  widgets.
    settings_dialog.py / proton_window.py / artwork_picker.py / log_window.py / theme.py
    tray.py        StatusNotifierItem + DBusMenu system-tray icon (pure Gio).
    login_base.py  `WebKitLoginDialog` base (embedded browser chrome).
    login_registry.py    source_id -> login-dialog mapping.
    steam/gog/epic_login_dialog.py  per-store WebKit sign-in; *control.py (unused).
    style/         Bundled CSS themes + brand SVGs.
```

---

## The two polymorphism axes

The refactor separates **two orthogonal axes** that were once conflated in `window.py`:

- **`Source` (per store)** — *where games come from + distribution lifecycle*:
  `sync()`, `sync_installed()`, `is_authenticated()`, `auth_store()`,
  `remember_account()`, `reset()`, `games_needing_artwork()`, `artwork_default`.
- **`GameEntry` (per game kind)** — *how a game is used once it's in the library*:
  the shared `on_launch()` skeleton with per-source hooks `launch / on_install /
  on_install_finished / on_stop / on_uninstall / install_dir / uninstall / store_url /
  record_exit`.

`entry_for(game, controller)` (in `domain/entry.py`) maps `game.source` to the right
`GameEntry` subclass, so `window.py` never switches on `game.source`.

---

## Key flows

### Launch (uniform Play button)

```
VitrineWindow.on_game_activated(game)
  -> entry_for(game).on_launch()                 # domain/entry.py
       if is_running(game):  entry.on_stop()      # click-while-running = Stop
       elif not installed and can_install(): entry.on_install()
       else: entry.launch()
```

Each `GameEntry.launch()` builds the concrete command via a **controller primitive**
(the window) and registers the running session:

- `LocalGameEntry` / `GogGameEntry` → `controller.launch_local(game)` →
  `Runtime.start(...)` (infra/running.py) with a `LaunchPlan` from `services/launch.py`.
- `EpicGameEntry` → builds legendary/umu command+env, then `controller.run_owned_launch`.
- `SteamGameEntry` → `controller.launch_steam(game)` (`steam://rungameid`) + a
  `SteamSessionWatcher` (infra/steamwatch.py).

The running game is tracked by the single `SessionManager` (`self.sessions`), which
drives the ticker ("Playing · h:mm") and unifies local/Steam/Epic state.

### Install

- Owned-but-not-installed store game → `entry.on_install()`.
- Epic: `EpicGameEntry._install_epic` → `legendary install ...` → `controller.start_install_command`.
- GOG: `GogGameEntry._install_gog` → gogdl `download`/`repair` → `controller.start_install_command`.
- Downloads run as `DownloadJob` (services/downloads.py); on completion the window's
  `_finish_download` calls `entry.on_install_finished(...)` (source-specific finish:
  Epic re-syncs installed state, GOG resolves the executable from its depot).

### Sync / login / reset (via `SyncService`)

- `SyncService.sync(source_id)` (services/sync.py) → `Source.sync()` + `sync_installed()`
  + `remember_account()`; returns a `SyncResult` + which games need artwork.
- Login dialogs are mapped by `ui/login_registry.py`; the window's
  `on_source_login(source_id)` opens the WebKit dialog, then syncs.
- `Source.reset()` clears credentials + stale library rows (used by reset-session).

### Artwork

- `services/artwork.py` `load_context()` snapshots credentials/priorities on the main
  thread; workers call `provider_candidates`/`fetch` (never touching sqlite).
- `ui/artwork_picker.py` shows per-provider tabs; `ui/window.py` fetches artwork for
  many games on a `ThreadPoolExecutor` off the UI thread.

### Playtime

- **local / GOG / Epic** → accumulate wall-clock hours locally
  (`Library.record_playtime`), via `GameEntry.record_exit` (base policy).
- **Steam** → Steam is authoritative; `SteamSource.refresh_playtime(game)` reads the
  local manifest (falling back to the Web API), then `Library.set_authoritative_playtime`
  *overwrites* (never accumulates). Handled by `SteamGameEntry.record_exit`.

### Stop

- `window._stop_game` → `entry_for(running).on_stop()`:
  - local/GOG → `Runtime.stop()` (proc-watch tree teardown);
  - Epic → kill the tracked legendary job/process;
  - Steam → `steamwatch.terminate_game`/`kill_game`.

---

## Threading / GUI boundary

- `domain/ services/ infra/ sources/` are GUI-free and safe to call from workers.
- Callbacks that must touch widgets go through `VitrineWindow._marshal(fn)` (a thin
  `GLib.idle_add`). Examples: `Runtime.on_exit`, `SteamSessionWatcher` start/exit,
  `DownloadJob.done`, and the steam playtime refresh result.
- The single sqlite connection lives on the main thread; `artwork.load_context()` and
  similar snapshot state for workers instead of handing them `Library`.

---

## Tests (tests/)

Each test file mirrors a module/area:

| Test | Covers |
| --- | --- |
| `test_domain_*`, `test_entries.py`, `test_session.py` | GameEntry dispatch, SessionManager, per-source playtime/stop |
| `test_launch.py`, `test_runners.py` | Launch plan, runner resolution |
| `test_running.py`, `test_procwatch.py`, `test_steamwatch.py` | Process supervision |
| `test_steam.py`, `test_gog.py`, `test_epic.py` | Source adapters + auth |
| `test_artwork*.py`, `test_d3d_extras.py`, `test_umu.py`, `test_gpu.py` | Services / infra |
| `test_sync.py`, `test_library.py`, `test_schema.py` | SyncService, repository, DB migrations |
| `test_ui_smoke.py`, `test_theme.py` | GUI imports (need GTK; excluded from the headless run) |

Run with `python -m pytest` (from the dev shell); the GUI tests are normally run
separately under `xvfb-run` (`tools/gui_smoke.py`).

---

## Repo / packaging

- `flake.nix` wires the NixOS runtime (GTK, WebKit, legendary, gogdl, umu) and the
  `VITRINE_*` env overrides used by the source adapters.
- `packaging/flatpak/` builds a shareable bundle on the GNOME runtime (see README).
- `pyproject.toml` discovers packages under `vitrine*`.