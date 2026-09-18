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
| `vitrine/sources/` | Source providers (`local` today; `steam` next) |
| `vitrine/ui/` | GTK4 + libadwaita front end |

## Development

```sh
nix develop
python -m vitrine          # launch the app
python -m pytest           # run the tests
ruff check .
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

## Roadmap

0. Shell, schema, source API
1. Local game + Wine/Proton launch + gamescope
2. Runner catalogue, download, per-game and global defaults
3. Per-game configuration UI (prefix, DLLs, DXVK, env, arguments)
4. Metadata and artwork (lutris.net, SteamGridDB, IGDB, manual override)
5. Steam source: owned library and Steam Family, with durable auth
6. Unified library view with deduplication and source ranking
7. GOG, Epic (via `legendary`), installs from Lutris installer scripts
