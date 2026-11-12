#!/bin/bash
set -euo pipefail
# Install first so the installer's login-shell self-tests use the local store.
install -m 644 /opt/box/shell.sh /etc/profile.d/box.sh
export NIX_REMOTE=local
export DETSYS_IDS_TELEMETRY=disabled
# The installer release embeds the matching Determinate Nix distribution.
test "$(uname -m)" = x86_64 || { echo "Box currently requires x86_64 Linux" >&2; exit 1; }
curl --fail --silent --show-error --location \
  "https://github.com/DeterminateSystems/nix-installer/releases/download/v3.24.1/nix-installer-x86_64-linux" \
  --output /tmp/nix-installer
chmod +x /tmp/nix-installer
/tmp/nix-installer install linux --no-confirm --init none \
  --extra-conf 'sandbox = false' --extra-conf 'eval-cores = 1' --diagnostic-endpoint ''
# Limit evaluation concurrency after intermittent container worker failures;
# build jobs remain parallel. This does not change the active generation on error.
rm /tmp/nix-installer
export PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH"
export USER=root LOGNAME=root HOME=/root
# Only fresh-image defaults are removed. Runtime boot never reactivates HM.
rm -f /root/.bashrc /root/.profile /root/.bash_profile
mkdir -p /root/.config/home-manager
cp -R /opt/box/home/. /root/.config/home-manager/
# Keep ordinary `git config --global` writes separate from HM's immutable file.
touch /root/.gitconfig
activation=$(nix build --no-update-lock-file --no-link --print-out-paths \
  path:/root/.config/home-manager#homeConfigurations.root.activationPackage)
# Convert the installer's legacy profile once, keeping Determinate and its CA
# package. Home Manager then owns this home-manager-path through the modern CLI.
nix profile add "$activation/home-path"
"$activation/activate"
# Preinstall public extensions/plugins without any runtime credentials.
pi update --extensions
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add zed-industries/delta-claude
claude plugin install typescript-lsp@claude-plugins-official
claude plugin install delta@zed-industries
# Installer commands may add machine-specific runtime state. Every new box gets
# its own identities; only the UI onboarding flag belongs in the base image.
printf '%s\n' '{"hasCompletedOnboarding":true}' > /root/.claude.json
chmod 600 /root/.claude.json
jq 'del(.deviceId)' /root/.pi/agent/settings.json > /tmp/pi-settings.json
install -m 600 /tmp/pi-settings.json /root/.pi/agent/settings.json
rm /tmp/pi-settings.json
# The official guard preserves Rex's terminal type on compatible SSH peers,
# without Home Manager taking ownership of the user's mutable ~/.ssh/config.
mkdir -p /etc/ssh/ssh_config.d
install -m 644 /root/.nix-profile/share/rex/ssh_config.d/50-rex.conf \
  /etc/ssh/ssh_config.d/50-rex.conf
# The slim base has no openssh package to seed the system configuration.
if [ ! -e /etc/ssh/ssh_config ]; then
  printf 'Include /etc/ssh/ssh_config.d/*.conf\n' > /etc/ssh/ssh_config
fi
# Tailscale's SSH login must use bash, including for noninteractive commands.
usermod --shell /bin/bash root
mkdir -p /root/.config/mise /var/lib/box/tailscale
chmod 700 /var/lib/box/tailscale
