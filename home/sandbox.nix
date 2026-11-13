# The home of an agent sandbox: the root user of the container image (image/Dockerfile).
{ pkgs, ... }:
{
  imports = [
    ./profiles/base.nix
    ./profiles/agents.nix
  ];

  home.username = "root";
  home.homeDirectory = "/root";
  home.stateVersion = "26.11";

  # The image is Ubuntu, not NixOS. A sandbox has no display, so no graphics drivers.
  targets.genericLinux.enable = true;
  targets.genericLinux.gpu.enable = false;

  # Nix itself, so that a sandbox can add packages and apply a newer version of this home.
  home.packages = [ pkgs.nix ];
  nix.package = pkgs.nix;
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    # A container has no build users, and it cannot nest the Nix build sandbox.
    build-users-group = "";
    sandbox = false;
  };

  # Keep the image small: no manual page index, and one locale in place of all of them.
  programs.man.enable = false;
  i18n.glibcLocales = pkgs.glibcLocales.override {
    allLocales = false;
    locales = [ "en_US.UTF-8/UTF-8" ];
  };

  box.agents.instructions = ./sandbox/AGENTS.md;

  # Repositories in a sandbox are often owned by another user than root.
  programs.git.settings.safe.directory = "*";

  # A sandbox has no terminal to answer prompts.
  programs.mise.globalConfig.settings.yes = true;
}
