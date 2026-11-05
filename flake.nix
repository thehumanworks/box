{
  description = "Modal sandboxes managed by nix and mise";

  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0.1";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f {
            inherit system;
            pkgs = nixpkgs.legacyPackages.${system};
          }
        );
      inherit ((builtins.fromTOML (builtins.readFile ./pyproject.toml))) project;
    in
    {
      packages = forAllSystems (
        { pkgs, ... }: {
          default = pkgs.python3Packages.buildPythonApplication {
            pname = project.name;
            inherit (project) version;
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./pyproject.toml
                ./src
                ./tests
              ];
            };
            build-system = [ pkgs.python3Packages.hatchling ];
            # Add Nix runtime dependencies here when the project grows.
            dependencies = [ ];
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              PYTHONPATH=src python -m unittest discover -s tests -v
              runHook postCheck
            '';
            pythonImportsCheck = [ "box" ];
            meta.mainProgram = "box";
          };
        }
      );

      apps = forAllSystems (
        { system, ... }: {
          default = {
            type = "app";
            meta.description = "uv example application";
            program = "${self.packages.${system}.default}/bin/box";
          };
        }
      );

      devShells = forAllSystems (
        { pkgs, ... }: {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.uv
              pkgs.ruff
              pkgs.dotenvx
              (import ./nix/secrets.nix { inherit pkgs; })
            ];
            UV_PYTHON = "${pkgs.python3}/bin/python3";
            UV_PYTHON_DOWNLOADS = "never";
            UV_LINK_MODE = "copy";
            PYTHONNOUSERSITE = "1";
          };
        }
      );

      checks = forAllSystems (
        { system, pkgs }: {
          package = self.packages.${system}.default;
          lock =
            pkgs.runCommand "uv-lock-check"
              {
                nativeBuildInputs = [ pkgs.uv ];
                UV_PYTHON = "${pkgs.python3}/bin/python3";
                UV_PYTHON_DOWNLOADS = "never";
                src = pkgs.lib.fileset.toSource {
                  root = ./.;
                  fileset = pkgs.lib.fileset.unions [
                    ./pyproject.toml
                    ./uv.lock
                  ];
                };
              }
              ''
                export HOME="$TMPDIR"
                export UV_CACHE_DIR="$TMPDIR/uv-cache"
                cd "$src"
                uv lock --check --offline
                touch "$out"
              '';
        }
      );

      formatter = forAllSystems ({ pkgs, ... }: pkgs.nixfmt-tree);
    };
}
