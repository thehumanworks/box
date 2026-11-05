# Shared by login, interactive, and noninteractive Bash shells.
export PATH="/root/.local/share/mise/shims:/root/.local/bin:/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
# Boxes run as root without systemd or a Nix daemon.
export NIX_REMOTE=local
# Crash reporting uses an unsupported container socket operation.
export DETSYS_IDS_TELEMETRY=disabled
export REX_RUNTIME_DIR=/run/box/rex
# Home Manager supplies the active generation's settings to every shell mode.
if [ -r /root/.nix-profile/etc/profile.d/hm-session-vars.sh ]; then
  # This hook resets PATH and may run through both BASH_ENV and .bashrc.
  # Reload the active generation rather than inheriting a parent's old guard.
  unset __HM_SESS_VARS_SOURCED
  # Home Manager's generic-Linux hooks expand optional variables such as
  # XCURSOR_PATH without nounset guards. Preserve the caller's shell option.
  case $- in
    *u*) set +u; . /root/.nix-profile/etc/profile.d/hm-session-vars.sh; set -u ;;
    *) . /root/.nix-profile/etc/profile.d/hm-session-vars.sh ;;
  esac
fi
# Nix's profile hook can prepend itself after Home Manager's sessionPath.
# Keep mise overrides ahead of the default tools in every entry path.
case "$PATH" in
  /root/.local/share/mise/shims:/root/.local/bin:*) ;;
  *) export PATH="/root/.local/share/mise/shims:/root/.local/bin:$PATH" ;;
esac
# Tailscale SSH does not forward the sandbox process environment.
if [ -r /run/box/ssh-env.sh ]; then
  . /run/box/ssh-env.sh
fi
