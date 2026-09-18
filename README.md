# Vitrine

A unified game library launcher for Linux: one library view across every store you own
games on (local installs, Steam owned + Steam Family, GOG and Epic later), with covers
normalised to a single aspect ratio instead of the mix of portraits, banners and icons
each store ships.

Status: **early scaffolding**. Nothing here launches a game yet.

## Layout

| Path | Purpose |
| --- | --- |
| `vitrine/db.py` | SQLite connection, schema, migrations |
| `vitrine/library.py` | `Game` model and library CRUD |
| `vitrine/paths.py` | XDG paths (`~/.local/share/vitrine`, `~/.cache/vitrine`) |
| `vitrine/launch.py` | Wine/Proton + gamescope command construction |
| `vitrine/running.py` | Process supervisor: start/stop/watch one game, playtime |
| `vitrine/sources/` | Source providers (`local` today; `steam` next) |
| `vitrine/ui/` | GTK4 + libadwaita front end |
| `vitrine/ui/theme.py` | Theme registry and manager (Galaxy / follow system) |
| `vitrine/ui/style/` | Bundled CSS themes (`.css` per theme) |
| `vitrine/ui/game_form.py` | Shared add/edit game form (fields + artwork pickers) |
| `vitrine/ui/game_dialogs.py` | Add-game and per-game settings windows (movable) |
| `vitrine/ui/game_detail_bar.py` | Collapsible hero detail bar (backdrop, play, playtime) |

## Development

```sh
nix develop             # enter the dev shell
python -m vitrine       # launch the app
python -m pytest        # run the tests
ruff check .
```

The app is also exposed as a flake app and package, with the runtime
environment (GTK, libadwaita and their typelibs) wired up automatically:

```sh
nix run .#              # run Vitrine without entering the dev shell
```

`tools/gui_smoke.py` builds the real window, database and library grid against a
throwaway XDG directory, then quits — useful as a fast end-to-end check:

```sh
nix develop -c sh -c 'Xvfb :99 -screen 0 1280x800x24 & sleep 1; DISPLAY=:99 python tools/gui_smoke.py'
```

## Design decisions

- **Own database.** Vitrine keeps its own SQLite library and never writes to
  `~/.local/share/lutris`. Lutris can be installed alongside.
- **No Lutris code.** Lutris is a reference implementation, not a dependency. The
  `lutris.net` public API is used for catalogue IDs, artwork and runner downloads.
- **Source plugins.** Each store is a `Source` subclass; syncs run off the UI thread and
  must not import GTK.
- **Normalised artwork.** Covers are cached at one fixed aspect ratio so the unified grid
  looks uniform regardless of origin (`~/.cache/vitrine/covers`).
- **One game at a time.** Launching is mediated by a supervisor that refuses to start a
  second game while one is running, and records playtime when a game exits.
- **Themes share one grammar.** Galaxy and follow-system themes keep the same window
  structure, placements and detail bar; only accents, corners, shapes and spacing vary.
- **Per-game artwork.** Games can opt into a portrait cover and a wide hero banner
  (essential for local games, which have no store artwork); the detail bar falls back to
  the cover behind a scrim, then initials.

## Roadmap

0. Shell, schema, source API
1. Local game + Wine/Proton launch + gamescope
   - Launch pipeline (gamescope / GameMode / MangoHud / DXVK / Wayland) — done
   - Process supervision, running indicator, playtime recording — done
2. Themed shell + settings
   - Theme engine (Galaxy default / follow system) + bundled CSS — done
   - Global settings (cog) and per-game settings (right-click / detail-bar cog) — done
   - Hero detail bar with play / playtime / last-played — done
   - Single titlebar for editors/settings; sources column fixed-width — done
   - Edition windows are movable; double-click launches, single click selects — done
   - Hero collapses via an on-image toggle and shrinks below its locked size — done
3. Runner catalogue, download, per-game and global defaults
3. Per-game configuration UI (prefix, DLLs, DXVK, env, arguments)
4. Metadata and artwork (lutris.net, SteamGridDB, IGDB, manual override)
5. Steam source: owned library and Steam Family, with durable auth
6. Unified library view with deduplication and source ranking
7. GOG, Epic (via `legendary`), installs from Lutris installer scripts
