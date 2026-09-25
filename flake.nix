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
      # Needs to be defined before use.
      d3dExtras = pkgs.stdenv.mkDerivation {
        pname = "d3d_extras";
        version = "v2";
        src = pkgs.fetchurl {
          url = "https://github.com/lutris/d3d_extras/releases/download/v2/v2.tar.xz";
          sha256 = "1bvkn1jvdrmgwj0l5fwbwgiv2f77g50k2xfnq1gqclvvjj3aq5wi";
        };
        dontConfigure = true;
        dontBuild = true;
        installPhase = ''
          mkdir -p "$out"
          tar -xJf "$src" --strip-components=1 -C "$out"
        '';
      };

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
        # Give the app a definite path to the bundled legendary so Epic
        # installs/launches always find it, independent of the caller's PATH.
        export VITRINE_LEGENDARY="${pkgs.legendary-gl}/bin/legendary"
        # Likewise pin gogdl (Heroic's GOG depot downloader) for non-interactive
        # GOG installs.
        export VITRINE_GOGDL="${pkgs.gogdl}/bin/gogdl"
        # Point at the bundled D3D runtime DLLs (DirectX 9/10/11 helpers) used to
        # populate Wine prefixes so old games launch.
        export VITRINE_D3D_EXTRAS="${d3dExtras}"
        # Unified launcher for Proton/GE-Proton (umu-run). It handles Proton
        # prefix setup (runtime DLLs, drives) and path mapping that we no longer
        # need to hand-roll.
        export VITRINE_UMU="${pkgs.umu-launcher}/bin/umu-run"
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
        paths = [ vitrineApp pkgs.legendary-gl pkgs.gogdl pkgs.umu-launcher d3dExtras ];
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
          pkgs.gogdl
          pkgs.umu-launcher
          d3dExtras
        ];

        shellHook = ''
          echo "NIX Dev Environment: Vitrine"
          export LD_LIBRARY_PATH="${runtimeEnv}:$LD_LIBRARY_PATH"
          export GI_TYPELIB_PATH="${typelibPath}:$GI_TYPELIB_PATH"
          export XDG_DATA_DIRS="${dataDirs}:$XDG_DATA_DIRS"
          export GST_PLUGIN_SYSTEM_PATH="${gstPluginPath}:$GST_PLUGIN_SYSTEM_PATH"
          export VITRINE_UMU="${pkgs.umu-launcher}/bin/umu-run"
          export VITRINE_D3D_EXTRAS="${d3dExtras}"
          export VITRINE_DEV=1
          mkdir -p "$PWD/.vscode"
          ln -sfn "$(command -v python)" "$PWD/.vscode/nix-python"
        '';
      };
    };
}