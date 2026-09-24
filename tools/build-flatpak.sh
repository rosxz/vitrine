#!/usr/bin/env bash
# Build a shareable Vitrine Flatpak bundle.
#
# Requires: flatpak, flatpak-builder (and the org.gnome.Platform//50 +
# org.gnome.Sdk//50 runtimes, which `flatpak --user install -y` via flathub).
#
# Output: dist-flatpak/io.github.crea.vitrine.flatpak
set -euo pipefail

cd "$(dirname "$0")/.."
MANIFEST=packaging/flatpak/io.github.crea.vitrine.yml
APP=io.github.crea.vitrine
BRANCH=stable
ARCH="$(uname -m)"
REPO=.flatpak-repo
BUILDDIR=.flatpak-build/
BUNDLE=dist-flatpak/$APP.flatpak

# GNOME runtime/Sdk (the app needs GTK4, libadwaita and WebKitGTK 6.0, which
# ship with org.gnome.Platform rather than org.freedesktop.Platform).
if ! flatpak --user list --runtime 2>/dev/null | grep -q "GNOME Application Platform version 50"; then
  echo "Installing org.gnome.Platform//50 + Sdk//50 (one-time)..."
  flatpak --user install -y flathub org.gnome.Platform/x86_64/50 org.gnome.Sdk/x86_64/50
fi

echo "Building (this can take a while)..."
flatpak-builder --repo="$REPO" "$BUILDDIR" "$MANIFEST" || {
  echo "Note: if the build failed only at 'appstreamcli compose' (missing runtime"
  echo "tool), the manual finish/export path below still works."
  echo "Running manual finish/export instead..."
  flatpak-builder --force-clean --build-only --repo="$REPO" "$BUILDDIR" "$MANIFEST"
  flatpak build-finish \
    --command=vitrine \
    --share=ipc \
    --socket=wayland \
    --socket=fallback-x11 \
    --device=dri \
    --share=network \
    --filesystem=home \
    --talk-name=org.freedesktop.Notifications \
    '--talk-name=org.freedesktop.portal.*' \
    '--talk-name=org.freedesktop.portal.Flatpak' \
    "$BUILDDIR"
  flatpak build-export --arch="$ARCH" --no-update-summary "$REPO" "$BUILDDIR" "$BRANCH"
}

echo "Bundling..."
mkdir -p dist-flatpak
flatpak build-bundle --arch="$ARCH" "$REPO" "$BUNDLE" "$APP" "$BRANCH"
echo
echo "Done: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
echo
echo "Friends install it with:"
echo "  flatpak --user install -y $BUNDLE"
echo "Your friends also need the same GNOME runtime (flatpak auto-suggests it)."