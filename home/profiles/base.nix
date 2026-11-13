# Command-line essentials for every home: computers and sandboxes.
#
# Put a tool here only if every home needs it. A tool for one project belongs in that
# project (mise, or `nix shell nixpkgs#<tool>`), not here.
{ pkgs, ... }:
{
  home.packages = with pkgs; [
    curl
    fd
    gh
    jq
    openssh
    ripgrep
    unzip
  ];

  # Bash loads the session variables and the tool hooks that Home Manager generates.
  programs.bash.enable = true;

  programs.git = {
    enable = true;
    settings.init.defaultBranch = "main";
  };

  # mise installs the tools that a project declares in its own mise.toml.
  # mise itself comes from Nix, so it must not look for its own updates.
  programs.mise = {
    enable = true;
    globalConfig.settings.disable_update_warning = true;
  };

  # The `home-manager` command, to apply a newer version of the home.
  programs.home-manager.enable = true;
}
