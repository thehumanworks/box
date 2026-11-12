{
  description = "Box user environment, managed by Home Manager and Determinate Nix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/6dadaf4375be35033e1c84395adbd53ff5fded96";
    home-manager = {
      url = "github:nix-community/home-manager/b787c48e2ec57a9d098f5c2721882a3bc1e16f97";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    rex = {
      url = "https://pkgs.superlogical.com/rex/nix/preview/0.1.0-preview.13.tar.gz";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, rex, ... }: {
    homeConfigurations.root = home-manager.lib.homeManagerConfiguration {
      pkgs = import nixpkgs {
        # Modal's development image runs on x86_64 Linux, including for Mac clients.
        system = "x86_64-linux";
        config.allowUnfreePredicate = pkg:
          builtins.elem (nixpkgs.lib.getName pkg) [ "1password-cli" "claude-code" ];
      };
      modules = [ rex.homeManagerModules.default ./home.nix ];
    };
  };
}
