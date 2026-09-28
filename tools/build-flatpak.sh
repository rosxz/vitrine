#!/usr/bin/env bash
# Build a shareable Vitrine Flatpak bundle.
#
# Requires: flatpak, flatpak-builder, appstream (for the appstreamcli compose
# step that finalises the metadata), and the org.gnome.Platform//50 +
# org.gnome.Sdk//50 runtimes.
#
# Output: dist-flatpak/io.github.rosxz.vitrine.flatpak
set -euo pipefail

cd "$(dirname "$0")/.."
MANIFEST=packaging/flatpak/io.github.rosxz.vitrine.yml
APP=io.github.rosxz.vitrine
BRANCH=master
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

# The normal flatpak-builder finish step runs `appstreamcli compose`; install it
# (via nix) so the metadata (add-extensions, multiarch) is finalised properly.
export PATH="$(nix-shell -p flatpak-builder -p appstream --quiet --run 'echo "$PATH"'):$PATH"

echo "Building (this can take a while)..."
flatpak-builder --force-clean --repo="$REPO" --default-branch="$BRANCH" "$BUILDDIR" "$MANIFEST"

echo "Bundling..."
mkdir -p dist-flatpak
flatpak build-bundle --arch="$ARCH" "$REPO" "$BUNDLE" "$APP" "$BRANCH"
echo
echo "Done: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
echo
echo "Friends install it with:"
echo "  flatpak --user install -y $BUNDLE"
echo "Your friends also need the same GNOME runtime (flatpak auto-suggests it)."