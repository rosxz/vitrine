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

      # Run Vitrine from the source tree with the runtime environment set.
      vitrineApp = pkgs.writeShellScriptBin "vitrine" ''
        export LD_LIBRARY_PATH="${runtimeEnv}:$LD_LIBRARY_PATH"
        export GI_TYPELIB_PATH="${typelibPath}:$GI_TYPELIB_PATH"
        export XDG_DATA_DIRS="${dataDirs}:$XDG_DATA_DIRS"
        exec ${runtime.python}/bin/python -m vitrine "$@"
      '';
    in
    {
      packages.${system}.default = pkgs.symlinkJoin {
        name = "vitrine";
        paths = [ vitrineApp ];
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
          pkgs.adwaita-icon-theme
          pkgs.hicolor-icon-theme
          pkgs.blueprint-compiler
          pkgs.gamescope
          pkgs.xvfb
          pkgs.ruff
          pkgs.mypy
        ];

        shellHook = ''
          echo "NIX Dev Environment: Vitrine"
          export LD_LIBRARY_PATH="${runtimeEnv}:$LD_LIBRARY_PATH"
          export GI_TYPELIB_PATH="${typelibPath}:$GI_TYPELIB_PATH"
          export XDG_DATA_DIRS="${dataDirs}:$XDG_DATA_DIRS"
          export VITRINE_DEV=1
        '';
      };
    };
}