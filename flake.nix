{
  description = "One environment for my machines and my agent sandboxes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      inherit (nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      # Every output uses the same package set, built with the same settings.
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config = import ./nix/nixpkgs-config.nix { inherit lib; };
        };

      forAllSystems = f: lib.genAttrs systems (system: f (pkgsFor system));

      # A home is one file in home/ that names a user and imports profiles.
      mkHome =
        system: module:
        home-manager.lib.homeManagerConfiguration {
          pkgs = pkgsFor system;
          modules = [ module ];
        };
    in
    {
      homeConfigurations = {
        sandbox = mkHome "x86_64-linux" ./home/sandbox.nix;
      };

      packages = forAllSystems (
        pkgs:
        lib.optionalAttrs (pkgs.stdenv.hostPlatform.system == "x86_64-linux") {
          # What the sandbox image installs. Build it with `nix build .#sandbox-home`.
          sandbox-home = self.homeConfigurations.sandbox.activationPackage;
        }
      );

      # `nix fmt` formats every Nix file in the repository.
      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);

      # `nix flake check` evaluates all outputs and runs these checks.
      checks = forAllSystems (pkgs: {
        format = pkgs.runCommand "format-check" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
          find ${self} -name '*.nix' -exec nixfmt --check {} +
          touch $out
        '';
      });

      # `nix develop` gives the tools to work on this repository.
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShellNoCC {
          packages = [
            pkgs.actionlint
            pkgs.nixfmt-tree
            pkgs.nodejs_24
          ];
        };
      });
    };
}
