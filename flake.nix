{
  description = "Vitrine - a unified game library launcher for Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      # Everything the app needs at runtime. The devShell adds tooling on top.
      runtime = {
        python = with pkgs.python3; withPackages (ps: [
          ps.pygobject3
          ps.requests
          ps.pillow
        ]);
        # glib is required: it ships the Gio/GObject/GLib typelibs that GTK's own
        # typelibs depend on, and without them PyGObject cannot create a real
        # GApplication. webkitgtk_6_0 is the GTK4 WebKit build that powers the
        # embedded Steam sign-in window (webkitgtk_4_1 is a GTK3 rebuild, so it
        # cannot coexist with GTK4 in one process); libsoup_3 supplies the
        # Soup-3.0 typelib WebKit's GI needs.
        libs = [
          pkgs.gtk4
          pkgs.libadwaita
          pkgs.glib
          pkgs.gobject-introspection
          pkgs.gdk-pixbuf
          pkgs.graphene
          pkgs.pango
          pkgs.harfbuzz
          pkgs.webkitgtk_6_0
          pkgs.libsoup_3
          # WebKit's embedded media stack needs GStreamer; missing plugins make
          # it spam 'appsink not found' and can correlate with crashes.
          pkgs.gst_all_1.gstreamer
          pkgs.gst_all_1.gst-plugins-base
          pkgs.gst_all_1.gst-plugins-good
          pkgs.gst_all_1.gst-plugins-bad
        ];
        themes = [ pkgs.adwaita-icon-theme pkgs.hicolor-icon-theme ];
      };

      runtimeEnv = pkgs.lib.makeLibraryPath (runtime.libs ++ runtime.themes);
      # Typelibs ship in the ``out`` output for every dependent, which is not
      # always the default output (pango's default is its ``bin`` output), so
      # reference ``.out`` explicitly.
      typelibs = runtime.libs ++ runtime.themes;
      typelibPath = pkgs.lib.concatStringsSep ":" (map (p: "${p.out}/lib/girepository-1.0") typelibs);
      dataDirs = pkgs.lib.concatStringsSep ":" (map (p: "${p}/share") (runtime.libs ++ runtime.themes));

      # Where GStreamer looks for its plugins (used by WebKit's media stack).
      gstPlugins = with pkgs.gst_all_1; [ gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad ];
      gstPluginPath = pkgs.lib.concatStringsSep ":" (map (p: "${p}/lib/gstreamer-1.0") gstPlugins);

      # Run Vitrine from the source tree with the runtime environment set.
      vitrineApp = pkgs.writeShellScriptBin "vitrine" ''
        # The packaged output is a symlinkJoin that also contains `legendary`;
        # put its bin/ on PATH so Epic installs/launches always find it.
        export PATH="$(dirname "$0"):$PATH"
        export LD_LIBRARY_PATH="${runtimeEnv}:$LD_LIBRARY_PATH"
        export GI_TYPELIB_PATH="${typelibPath}:$GI_TYPELIB_PATH"
        export XDG_DATA_DIRS="${dataDirs}:$XDG_DATA_DIRS"
        export GST_PLUGIN_SYSTEM_PATH="${gstPluginPath}:$GST_PLUGIN_SYSTEM_PATH"
        exec ${runtime.python}/bin/python -m vitrine "$@"
      '';
    in
    {
      packages.${system}.default = pkgs.symlinkJoin {
        name = "vitrine";
        paths = [ vitrineApp pkgs.legendary-gl ];
        passthru.python = runtime.python;
      };

      apps.${system}.default = {
        type = "app";
        program = "${vitrineApp}/bin/vitrine";
      };

      devShells.${system}.default = pkgs.mkShell {
        buildInputs = [
          runtime.python
          (pkgs.python3.withPackages (ps: [ ps.pytest ]))
          pkgs.gtk4
          pkgs.libadwaita
          pkgs.glib
          pkgs.gobject-introspection
          pkgs.gdk-pixbuf
          pkgs.graphene
          pkgs.pango
          pkgs.harfbuzz
          pkgs.webkitgtk_6_0
          pkgs.libsoup_3
          pkgs.gst_all_1.gstreamer
          pkgs.gst_all_1.gst-plugins-base
          pkgs.gst_all_1.gst-plugins-good
          pkgs.gst_all_1.gst-plugins-bad
          pkgs.adwaita-icon-theme
          pkgs.hicolor-icon-theme
          pkgs.blueprint-compiler
          pkgs.gamescope
          pkgs.xvfb
          pkgs.ruff
          pkgs.mypy
          pkgs.legendary-gl
        ];

        shellHook = ''
          echo "NIX Dev Environment: Vitrine"
          export LD_LIBRARY_PATH="${runtimeEnv}:$LD_LIBRARY_PATH"
          export GI_TYPELIB_PATH="${typelibPath}:$GI_TYPELIB_PATH"
          export XDG_DATA_DIRS="${dataDirs}:$XDG_DATA_DIRS"
          export GST_PLUGIN_SYSTEM_PATH="${gstPluginPath}:$GST_PLUGIN_SYSTEM_PATH"
          export VITRINE_DEV=1
        '';
      };
    };
}