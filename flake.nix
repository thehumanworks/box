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
        { pkgs, system }:
        let
          # nixpkgs currently has Modal 1.6.2; use the release pinned by uv.
          modalLock = builtins.head (
            builtins.filter (p: p.name == "modal") (builtins.fromTOML (builtins.readFile ./uv.lock)).package
          );
          modal = pkgs.python3Packages.buildPythonPackage {
            pname = "modal";
            inherit (modalLock) version;
            pyproject = true;
            src = pkgs.fetchurl {
              inherit (modalLock.sdist) url;
              hash = modalLock.sdist.hash;
            };
            build-system = with pkgs.python3Packages; [
              setuptools
              wheel
            ];
            inherit (pkgs.python3Packages.modal) dependencies;
            pythonImportsCheck = [ "modal" ];
          };
        in
        {
          default = self.packages.${system}.unwrapped;

          with-secrets = pkgs.writeShellApplication {
            name = "box";
            text = ''
              exec ${pkgs.dotenvx}/bin/dotenvx run --quiet --strict \
                -f ${./.env} \
                -- ${self.packages.${system}.unwrapped}/bin/box "$@"
            '';
            meta.description = "Box with bundled encrypted secrets resolved by dotenvx Armor at runtime";
          };

          unwrapped = pkgs.python3Packages.buildPythonApplication {
            pname = project.name;
            inherit (project) version;
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./pyproject.toml
                ./README.md
                ./src
                ./tests
                ./scripts
              ];
            };
            build-system = [ pkgs.python3Packages.hatchling ];
            dependencies = [
              modal
              pkgs.python3Packages.click
            ];
            nativeCheckInputs = [
              pkgs.python3Packages.pytestCheckHook
              pkgs.python3Packages.fastapi
              pkgs.python3Packages.pydantic
              pkgs.python3Packages.httpx
              pkgs.git
            ];
            doCheck = true;
            preCheck = ''
              # The remote Debian paths do not exist in the Nix build sandbox.
              # Patch only the test/source tree after installation, not the package.
              substituteInPlace tests/test_environment.py tests/test_home_manager.py tests/test_lifecycle.py \
                src/box/environment.py src/box/service.py \
                --replace-warn /bin/bash ${pkgs.bash}/bin/bash \
                --replace-warn /usr/bin/env ${pkgs.coreutils}/bin/env
              export PYTHONPATH="$PWD/src:$PYTHONPATH"
            '';
            pythonImportsCheck = [
              "box"
              "box.cli"
            ];
            meta.mainProgram = "box";
          };
        }
      );

      apps = forAllSystems (
        { system, ... }: {
          default = {
            type = "app";
            meta.description = "Modal sandboxes for humans and agents";
            program = "${self.packages.${system}.default}/bin/box";
          };
          with-secrets = {
            type = "app";
            meta.description = "Box with bundled encrypted secrets";
            program = "${self.packages.${system}.with-secrets}/bin/box";
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
            shellHook = ''
              # Keep pip-style commands away from the immutable Nix interpreter.
              unset UV_PYTHON
              if [ ! -d .venv ]; then
                uv venv --python "${pkgs.python3}/bin/python3" .venv
              fi
              source .venv/bin/activate
            '';
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
