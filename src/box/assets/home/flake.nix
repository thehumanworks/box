{
  description = "Box user environment, managed by Home Manager and Determinate Nix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/0bb2eedc20e4739f29e8045034f2828ad7054f8f";
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
          nixpkgs.lib.getName pkg == "1password-cli";
      };
      modules = [ rex.homeManagerModules.default ./home.nix ];
    };
  };
}
