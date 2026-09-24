#!/bin/sh
# Flatpak entrypoint for Vitrine. The GNOME runtime already provides
# python3 + PyGObject + GTK4 + libadwaita + WebKitGTK 6; we add the bundled
# CLI tools and python deps onto the app's own paths.
set -e

export VITRINE_LEGENDARY=/app/bin/legendary
export VITRINE_GOGDL=/app/bin/gogdl
export VITRINE_UMU=/app/bin/umu-run
export VITRINE_D3D_EXTRAS=/app/share/vitrine/d3d_extras

export PYTHONPATH=/app/share/vitrine
export PATH=/app/bin:$PATH

exec /usr/bin/python3 -m vitrine "$@"