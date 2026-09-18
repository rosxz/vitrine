{
  description = "Vitrine - a unified game library launcher for Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      python = pkgs.python3.withPackages (ps: [
        ps.pygobject3
        ps.requests
        ps.pillow
        ps.pytest
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        buildInputs = [
          python
          pkgs.gtk4
          pkgs.libadwaita
          pkgs.gobject-introspection
          pkgs.gdk-pixbuf
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
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.gobject-introspection
            pkgs.gdk-pixbuf
          ]}:$LD_LIBRARY_PATH"
          export GI_TYPELIB_PATH="${pkgs.gtk4}/lib/girepository-1.0:${pkgs.libadwaita}/lib/girepository-1.0:$GI_TYPELIB_PATH"
          export XDG_DATA_DIRS="${pkgs.gtk4}/share:${pkgs.libadwaita}/share:${pkgs.adwaita-icon-theme}/share:${pkgs.hicolor-icon-theme}/share:$XDG_DATA_DIRS"
          export VITRINE_DEV=1
        '';
      };
    };
}
