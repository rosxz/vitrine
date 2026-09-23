# Research — non-trivial fixes, changes & variables

This file records hard-won technical findings for the Vitrine project (a
GTK4/libadwaita game-library launcher for Steam/GOG/Epic/Wine-Proton on NixOS
Wayland). It exists so the *why* and the *non-obvious variables* behind fixes
aren't lost. Nothing here is trivial; treat it as the collective memory of the
launch/install/Wine pipeline.

Guidance shorthand used throughout the project:
- **"Always research Lutris, default to the Lutris approach"** — Lutris solved
  almost every Wine/Proton/NixOS integration problem before us. When in doubt,
  mirror what Lutris/Steam/Heroic do with the bundle and only diverge with
  evidence.

---

## 1. Architecture invariants

- The **engine** package (`db`, `library`, `launch`, `running`, `sources`,
  `util`, `runners.py`, `prefix.py`, `downloads.py`, `gpu.py`, `wine/`) must
  remain **GUI-free**. Only `ui/` + `application.py` may import GTK.
- Windows/editors/settings/login are plain `Gtk.Window` + `vitrine-window`
  class, **not** `Adw.Window` (project default for movable editor windows).
- Embedded WebKit login for Steam/GOG/Epic uses GI `WebKit 6.0`.
- Epic backend = **legendary CLI** (install/auth only; not needed for play).
  GOG install via depot (`gogdl`) with offline-installer fallback; **no Galaxy
  client**.
- Proton runs under `steam-run` (NixOS FHS bwrap). The system NixOS wine is
  Wayland-only (no `winex11.drv`), so games that need an X11 window must use a
  Proton runner.

---

## 2. GPU driver discovery — `vitrine/gpu.py`

Runtime discovery of the active Mesa stack so the fix survives system rebuilds
(`NixosDriverEnv` dataclass):

- Identifies the active Mesa ANV ICD via `intel_icd.x86_64.json` **NOT**
  `intel_hasvk` — `intel_hasvk` fails on Intel UHD 620.
- Surfaces `VK_ICD_FILENAMES`, `LD_LIBRARY_PATH` (mesa + vulkan-loader +
  libglvnd store dirs), `LIBGL_DRIVERS_PATH`, `MESA_DRIVER_PATH`.
- Freetype `LD_PRELOAD` was **removed**: a 64-bit `libfreetype` preloaded into
  wine's 32-bit loader aborts with `wrong ELF class`. Rely on FHS/steam-run for
  FreeType, never preload.
- `driver_env()`'s `LD_LIBRARY_PATH` **breaks pressure-vessel** (`pv-adverb:
  Cannot create temporary directory ... mkdirat: No such file or directory`) when
  applied to the umu env inside steam-run. Only the **non-loader** vars
  (`VK_ICD_FILENAMES`/`LIBGL_DRIVERS_PATH`/`MESA_DRIVER_PATH`) are safe to carry
  into Proton launches.

### ⚠️ Critical regression found & fixed — `VK_ICD_FILENAMES` kills game windows

**Finding:** Injecting `VK_ICD_FILENAMES` (the Nix mesa ICD) into the
umu/Proton environment made games **run but never present a window** (the exact
game-died-from-Vitrine bug).

**Isolation results** (Super Meat Boy, Proton-Experimental, `steam-run umu-run`):

| Env | Window? |
|---|---|
| clean `umu_env` (no extras) | ✅ fullscreen window |
| + d3d `WINEDLLOVERRIDES` (DLLs installed) | ✅ |
| + `LIBGL_DRIVERS_PATH` / `MESA_DRIVER_PATH` | ✅ |
| + **`VK_ICD_FILENAMES`** | ❌ game exits after `fsync`, no window |
| d3d + `VK_ICD_FILENAMES` | ❌ |

**Root cause:** pointing the Vulkan loader at the Nix `intel_icd.x86_64.json`
that pressure-vessel does **not** stage inside its steam-run FHS sandbox makes
DXVK fail to init, so the game aborts right after `fsync: up and running.`
Lutris/Steam/Heroic let pressure-vessel resolve its own drivers.

**Fix:**
- `vitrine/ui/window.py` Proton branch must **never** surface
  `VK_ICD_FILENAMES`; GL driver paths are fine to keep for legacy wined3d.
- `vitrine/wine/umu.py` `_UMP_PASSTHROUGH` — `VK_ICD_FILENAMES` must **not** be
  passed through so a parent env export can't leak it in.
- Regression test: `tests/test_umu.py::test_umu_env_never_leaks_vk_icd_filenames`.

---

## 3. Proton game window presentation (umu / steam-run)

- Game drives keep the process alive: `STEAM_COMPAT_INSTALL_PATH`/
  `STEAM_COMPAT_MOUNTS` = the real game dir (`os.path.dirname(exe)`).
  `cwd` = game dir; `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY` must be passed
  through. Without the compat vars the game dies ("game drive" mapping fails).
- `XAUTHORITY` fallback (`_discover_xauthority`): GUI-launched Vitrine often
  lacks `XAUTHORITY`. Discover `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` then
  `~/.Xauthority` (prefer prefix "mutter" for read permissions). When `DISPLAY`
  is set but `XAUTHORITY` unset, this is what lets the X11 window map to
  Xwayland.
- `steamrt4` (~400MB, `~/.local/share/umu/steamrt4`) auto-downloads on first
  run; `steamrt3` only if PROTONPATH missing. `umu` 1.4.4 from nixpkgs
  (`pkgs.umu-launcher`); Lutris's own `~/.local/share/lutris/runtime/umu` is the
  same version and shares steamrt4.
- Gamescope is **opt-in per game**, NOT default for Proton. Both gamescope
  tests ended `Primary child shut down!` / `Broken pipe`. The form that presents
  a window on Wayland is plain `steam-run umu-run <exe>` with clean env + game
  cwd.
- `Proton: Error: unable to use parent for game drive, path /home` is
  non-fatal noise (game isn't under a steamapps library path).
- Known-good spawn pattern (used by scripts and the code):
  `os.environ.pop("LD_LIBRARY_PATH")` / `STEAM_RUNTIME_LIBRARY_PATH`, then
  `umu.umu_env(...)`, `umu.umu_command(exe)`, `cwd=dirname(exe)`.
- NixOS-specific: Lutris ships an FHS bwrap (`lutris-...-fhsenv-rootfs`);
  pressure-vessel needs an ld cache staged for **both** 32/64 glibc.
- Epic executable resolution uses `lg.installed_executable(app)`
  (`install_path/executable`). Launch does **NOT** use legendary `--wrapper` /
  `--no-wine` (unreliable under steam-run); it uses direct `umu-run`.

---

## 4. Proton prefix handling — `vitrine/prefix.py`

- `_seed_from_proton` copies `files/share/default_pfx` (fresh directives),
  re-aims builtin-DLL symlinks absolutely into the dist, and creates DOS drives
  via `_create_dos_drives` (`c:`→`../drive_c`, `z:`→`/`). Fixes
  `could not load kernel32.dll` and `unable to use parent for game drive`.
- Note: for umu-managed Proton games a manual `wineboot` is unnecessary and
  fights umu (umu performs its own full prefix setup).

---

## 5. d3d_extras — `vitrine/wine/d3d_extras.py`

- Bundles Microsoft DirectX 9/10/11 runtime DLLs (d3dx9_*, d3dcompiler_*) from
  Lutris's `d3d_extras` release (v2 tarball, pinned flake hash
  `1bvkn1jvdrmgwj0l5fwbwgiv2f77g50k2xfnq1gqclvvjj3aq5wi`).
- Idempotent install to prefix `system32`/`syswow64`; **replaces Proton builtin
  symlink stubs with real DLLs** (a symlink → Proton's builtin stub is *not* a
  working runtime DLL, so symlinks always get replaced; real files are left).
- Marks DLLs `native` via `WINEDLLOVERRIDES`; wired into `launch.build_env` +
  `_launch_epic_game` under config `d3d_extras` (default on).
- Overrides installed into a real prefix with the DLLs present are safe (verified
  they do NOT break the window — see §2 table).

---

## 6. Sources

### Steam
- `sync_installed` reconciles the DB flag against `installed_on_disk()` (app
  manifests) so a played-but-uninstalled game isn't wrongly marked installed.

### GOG
- Torch state is Vitrine-owned (gogdl has no metadata). Install via gogdl depot
  to `data_dir/gog/<slug>`; `find_game_dir` for nested `goggame-*.info`; exe from
  `tasks[]` FileTask. Token refresh in `gog/auth.py` +
  `GogSource.ensure_fresh_token()`. `gog_install_dir` stored in `game.config`.
- Offline-installer fallback when gogdl stalls (bad token → 0 bytes, no output).
  Short **idle** timeout (`GOGDL_DOWNLOAD_TIMEOUT = 30.0`) kills stalled gogdl
  but never cuts a healthy download (healthy ones stream progress).

### Epic
- Legendary 0.21 reports owned games with `app_title` (not `title`);
  `_from_legendary` reads `app_title` fallback `title` — fixed "Epic refreshed 0
  games" (272 parsed).
- Embedded-WebKit auth: `authorizationCode` → legendary `--code` first, then
  `user.json`.
- `_dump_launch` writes `$XDG_CACHE_HOME/vitrine/proton-launch.env` — **now a
  settings toggle `dump_launch_env`, off by default**.

---

## 7. Downloads manager — `vitrine/downloads.py`

- `DownloadJob`/`run_download` support `timeout` = **idle** timeout (no output →
  kill; healthy downloads not cut) and `cwd`; stdin is `DEVNULL`.
- `run_download` streams lines via `on_line` for the log window.

---

## 8. UI / window behaviours (recent changes)

### Debug / log routing
- `DEBUG_LOG_SETTING = "auto_show_debug_log"` (`MirrorlySwitch S...`). When on,
  installs AND launches open an `ExecutionLogWindow`.
- **Virtualised grid** (`library_view.py`): only a leading window of tiles
  (`_INITIAL_BATCH=72`) is created synchronously on `set_games`; the rest stream
  in via `_do_fill` (one `_SCROLL_BATCH=72` per frame) as the viewport approaches
  the end (`_maybe_fill`), so switching to huge sources ("All games"/Steam) never
  stalls building every tile. `total_count()` reports the full view;
  `remove_game` also drops from the unbuilt pool. Combined with lazy cover
  decoding (`_reveal_visible`), both widget creation *and* artwork are on demand.
- Proton launches previously used inherited stdio (window worked but output was
  invisible). **Now:** when the debug log is open, the game's stdout+stderr are
  captured (`subprocess.PIPE`, `stderr=STDOUT`) and streamed line-by-line into
  the log window via `_watch_proton_proc`. When the log is closed, stdio stays
  inherited so a detached game never blocks on a full pipe (preserves working
  window-presentation).

### Env dump toggle
- `DUMP_LAUNCH_ENV_SETTING = "dump_launch_env"` — writes
  `proton-launch.env` (command + full env) on each Proton launch. **Off by
  default** (was unconditional). Exposed as "Dump launch environment to file" in
  Settings → Appearance.

### Right-click context menu (per game)
- "Open installation directory…" → `_open_install_dir` via `_game_install_dir`
  (Epic → `lg.installed_executable` dirname; else `game.executable` /
  `game.working_dir`).
- "Open prefix directory…" → `_open_prefix_dir` via `wine_prefix_for(game)`.
- Both use `_open_directory` (`xdg-open`), toasting if the dir is missing.

### Remove vs Uninstall
- **Local games**: remove DB row entirely.
- **Steam/GOG/Epic**: `Adw.AlertDialog` asks whether to also delete the prefix;
  then uninstalls on disk but **keeps** the library entry and sets
  `installed=False` → reverts to *available, not installed*. Label is
  "Uninstall…" for store games. Epic uses `legendary.uninstall(app)` (new) so
  legendary forgets the app; GOG/Steam remove the install dir
  (`shutil.rmtree`). Prefix deleted only on confirm.

---

## 9. Variables reference (env / settings / flags)

Bundles (set by `flake.nix`):
- `VITRINE_LEGENDARY` → `pkgs.legendary-gl/bin/legendary`
- `VITRINE_GOGDL` → `pkgs.gogdl/bin/gogdl`
- `VITRINE_UMU` → `pkgs.umu-launcher/bin/umu-run`
- `VITRINE_D3D_EXTRAS` → unpacked d3d_extras archive root (`x32/`, `x64/`)
- `VITRINE_DEV=1` in the dev shell

umu passthrough (`_UMP_PASSTHROUGH` in `vitrine/wine/umu.py`) — the ONLY vars
that reach pressure-vessel from the parent env. **`VK_ICD_FILENAMES` must not be
listed** (see §2). Core umu vars always forced by `umu_env`:
- `GAMEID`, `WINEPREFIX`, `WINEARCH=win64`, `PROTON_VERB=waitforexitandrun`
- `PROTONPATH`, `STEAM_COMPAT_DATA_PATH` (= WINEPREFIX),
  `STEAM_COMPAT_INSTALL_PATH`, `STEAM_COMPAT_MOUNTS` (= install dir)
- `PATH` = `/usr/bin:/bin:/run/current-system/sw/bin`

Settings keys (`vitrine/library.py`):
- `DEBUG_LOG_SETTING = "auto_show_debug_log"`
- `DUMP_LAUNCH_ENV_SETTING = "dump_launch_env"`
- Window-local: `HIDE_NOT_INSTALLED = "hide_not_installed"`
- Steam: `FAMILY_SETTING = "include_steam_family"` (in `steam_source.py`)

Per-game config (`DEFAULT_CONFIG` in `vitrine/library.py`): `graphics` ("x11"/
"wayland"), `gamemode`, `mangohud`, `gamescope`, ... plus `wine_binary`,
`runner`, `d3d_extras`.

Global runner settings: sidebar "Default Proton" (`DEFAULT_PROTON_SETTING` in
`runners.py`); per-game `runner` override wins, then sidebar default, then merged
global default.

Data layout (`vitrine/paths.py`, all under XDG, never inside Lutris):
- `$XDG_DATA_HOME/vitrine/`  → `library.db`, `runners/`, `prefixes/`
- `$XDG_CACHE_HOME/vitrine/` → `covers/`, `secrets/`, logs
- `$XDG_CONFIG_HOME/vitrine/` → user settings
- GOG installs live in `data_dir/gog/<slug>`.

---

## 9b. Artwork providers (IGDB + SteamGridDB + Lutris + Steam CDN)

New module tree: `vitrine/artwork_providers/` with a GUI-free `net.py` (shared
`_RATE_LIMITER`, `get_bytes`/`get_json`/`post_json`, `USER_AGENT`), a `base.py`
(`Art` candidate with `provider/slot/url/thumb`, `CandidateSet`), and one module
per provider: `igdb.py`, `steamgriddb.py`, `lutris.py`, `steam.py`. `artwork.py`
is the orchestrator (now priority-driven).

- **Game identity**: SteammdDB and IGDB match Steam games by `source_id` appid
  (authoritative); GOG/Epic fall back to name search (imperfect — the picker lets
  the user choose visually).
- **IGDB** needs Twitch Client-ID+Secret exchanged (client-credentials) for a
  short-lived `access_token`; cached/refreshed. Tile from `cover.image_id`
  (`cover_big_2x`); heroes from `artworks`/`screenshots` (`1080p`/`720p`).
  Image URL: `images.igdb.com/.../t_<size>/<id>.jpg`. Respects shared rate limiter.
  - ⚠️ **The `t_` prefix must NOT be in the size slug** — `images.igdb.com`
    already prepends `t_`. Using `t_cover_big_2x` produced `t_t_cover_big_2x`
    URLs that 404'd, making "IGDB empty for everything". Use `cover_big_2x`.
  - **Fall back to name search** when the Steam-appid `external_games.uid`
    match misses (old/niche Steam titles) — otherwise IGDB contributes nothing
    for many Steam games.
- **SteamGridDB** needs a Bearer API key. Heroes/grids via `/heroes/game/{id}` and
  `/grids/game/{id}?types=static&dimensions=600x900`.
- **Credentials**: `artwork.load_credentials(library)` merges env
  (`VITRINE_IGDB_CLIENT_ID`/`-SECRET`, `VITRINE_STEAMGRIDDB_KEY`) over settings
  keys. **Lutris & Steam always usable (no key).**
- **Priority** (`DEFAULT_TILE_PRIORITY`/`DEFAULT_HERO_PRIORITY`): tile = IGDB,
  SteamGridDB, Steam, Lutris; hero = **SteamGridDB first** (best heroes), then
  IGDB, Steam, Lutris. Overridable in Settings → Appearance (reorderable lists).
- **Cached artwork resolution is user-tunable** (Settings → Appearance
  "Cached resolution"): per-kind (tile/hero) longest-side pixel cap threaded via
  `ctx["tile_dim"]`/`ctx["hero_dim"]`. `PIL.Image.thumbnail` never upscales, so
  the value is automatically **capped by the source image** (its own resolution
  or the crop), satisfying "capped at the actual image / proportional ratio".
- **Default source is now `"auto"`** (was `"lutris"`), walking the per-slot
  priority chain; first provider that yields that slot wins. One-time hint
  (`artwork_provider_hint_seen`) surfaces when neither IGDB nor SteamGridDB key is
  configured (Lutris/Steam still work silently).
- **Refresh skips games that already have artwork** (default), so a source
  refresh never overwrites a user's choice. A global setting
  `refresh_all_artwork` (`FORCE_REFRESH_SETTING`, off by default — Settings →
  Appearance → "Refresh artwork for all games") makes the automatic source
  refresh re-pull **every** game's artwork (threaded through `ctx["force_refresh"]`).
  The per-game "Refresh artwork…" button already forces for a single title.
- **Picker** (`vitrine/ui/artwork_picker.py`): per-game "Choose artwork…" window
  shows thumbnails per provider, grouped; tile and hero slots are **mutually
  exclusive radio groups** across all providers; Apply caches via `artwork.fetch`
  and persists. Refreshes the GameForm cover/banner fields so a later Save doesn't
  clobber the choice (picker is launched from the dialog, which owns the form).

## 10. Test / lint

- 219+ tests; ruff clean. GUI tests (`test_ui_smoke`, `test_epic`, etc.) need
  PyGObject (`gi`) and `requests`, which the minimal `pytest` runner (the
  dev-shell `pkgs.python3.withPackages [pytest]`) lacks — run those from a
  GUI-capable env.
- Run in dev shell: `PYTHONPATH=<repo> pytest <tests> -q`.