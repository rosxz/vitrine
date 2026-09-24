# Vitrine

A unified game library launcher for Linux: one library view across every store you own
games on (local installs, Steam owned + Steam Family, GOG and Epic), with customizable
covers and banners normalised to a single aspect ratio.

## Layout

| Path | Purpose |
| --- | --- |
| `vitrine/db.py` | SQLite connection, schema, migrations |
| `vitrine/library.py` | `Game` model and library CRUD |
| `vitrine/paths.py` | XDG paths (`~/.local/share/vitrine`, `~/.cache/vitrine`) |
| `vitrine/launch.py` | Wine/Proton + gamescope command construction |
| `vitrine/running.py` | Process supervisor: start/stop/watch one game, playtime |
| `vitrine/sources/` | Source providers (`local`, `steam`) |
| `vitrine/sources/steam/` | Steam: VDF parsing, install discovery, durable auth cache |
| `vitrine/ui/steam_login_dialog.py` | Embedded WebKit sign-in (webkitgtk_6_0) |
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

## Flatpak

A shareable Flatpak bundle is prebuilt at `dist-flatpak/io.github.crea.vitrine.flatpak`.
Friends install it with:

```sh
flatpak --user install -y io.github.crea.vitrine.flatpak
flatpak run io.github.crea.vitrine
```

It bundles legendary (Epic), gogdl (GOG), umu-launcher and the d3d_extras Wine
runtime, and ships alongside the `org.gnome.Platform` runtime (which provides
GTK4, libadwaita and WebKitGTK 6). GPU drivers come from the system via
Flatpak's Mesa/GL extension (just needs `--device=dri`). Rebuild it with:

```sh
tools/build-flatpak.sh
```

**Steam works out of the box.** A `steam://rungameid/<appid>` URI is handed to
the host's own Steam through the OpenURI portal; the game actually runs on the
host (with the host's Proton and GPU), so Vitrine never needs to launch Wine in
the sandbox. The source reads the host Steam library under `~/.local/share/Steam`
(and the Flatpak Steam at `~/.var/app/com.valvesoftware.Steam/...`), all covered
by `--filesystem=home`.

## Design decisions

- **Own database.** Vitrine keeps its own SQLite library.
- **Lutris Reference.** Lutris is great, and used as reference implementation. Vitrine seeks
  to encourage the continued support of the Lutris project, through script installs, game database, etc.
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

## Future additions

- Support for Linux games (.sh)
- Game metadata (? consider feature)
- Game deduplication
- Support for Lutris installer scripts 
- Integrate Discord Rich Presence
- Proper Steam time tracking
- Game achievements for any source (GOG, Steam, Epic)
