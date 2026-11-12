# This is your editable environment configuration.
# Run `home-manager build` to check changes, then `home-manager switch` to apply.
# Run `home-manager generations` to inspect history and
# `home-manager switch --rollback` to restore the previous generation.
# Runtime credentials belong in Modal secrets, never in this directory.
{ lib, pkgs, ... }:
{
  imports = [ ./pi.nix ./claude.nix ];

  home.username = "root";
  home.homeDirectory = "/root";
  # Compatibility baseline; do not bump this merely to update packages.
  home.stateVersion = "26.05";

  # The image's installer owns Determinate Nix and its configuration.
  nix.enable = false;
  nix.package = null;
  targets.genericLinux.enable = true;
  targets.genericLinux.gpu.enable = false;
  programs.home-manager.enable = true;
  programs.rex = {
    enable = true;
    # Install terminfo, but leave SSH and init.lua writable by the user.
    sshGuard = false;
  };
  # Box's boot process supervises Rex; Modal has no systemd user manager.
  services.rex.enable = false;

  home.packages = with pkgs; [
    coreutils findutils gnugrep gnused gawk
    ripgrep fd jq tmux neovim less which
    curl wget openssh rsync unzip zip gnutar gzip
    gcc gnumake pkg-config
    python314 nodejs_24 uv tailscale _1password-cli
  ];

  home.sessionVariables = {
    EDITOR = "nvim";
    VISUAL = "nvim";
    NIX_REMOTE = "local";
    NIX_SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
    DETSYS_IDS_TELEMETRY = "disabled";
  };
  home.sessionPath = [
    "$HOME/.local/share/mise/shims"
    "$HOME/.local/bin"
  ];

  programs.bash = {
    enable = true;
    # This runs before Home Manager's noninteractive-shell return. Tailscale SSH
    # invokes Debian /bin/bash, which reads .bashrc for remote commands.
    bashrcExtra = ''. /etc/profile.d/box.sh'';
    # genericLinux sources nix.sh again in interactive shells, after mise.
    initExtra = lib.mkAfter ''
      export PATH="$HOME/.local/share/mise/shims:$HOME/.local/bin:$PATH"
    '';
  };
  programs.git = {
    enable = true;
    lfs.enable = true;
    settings.init.defaultBranch = "main";
    # Author identity stays in writable ~/.gitconfig, outside the Nix store.
  };
  programs.mise = {
    enable = true;
    enableBashIntegration = true;
    # `mise use --global` remains usable; HM owns only its own config fragment.
    mutableSettings = true;
  };
}
