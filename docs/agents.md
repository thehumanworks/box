# Pi and Claude Code in new boxes

## Image and configuration

The standalone Home Manager flake under `src/box/assets/home/` installs Pi and
Claude Code using their native Home Manager modules. `pi.nix` and `claude.nix`
mirror the host's declarative configuration plus its current portable UI/model
preferences. The nixpkgs revision pins Pi **1.5.0** and Claude Code **2.1.318**
(the host inspected during implementation used Claude **2.1.317**).

The image preinstalls all 15 declared Pi packages and the two enabled Claude
plugins (Delta and TypeScript LSP), including the language server. Pi's bridge
points to the Nix Claude executable, not the Agent SDK's bundled binary. Core
binaries are pinned; extensions/plugins intentionally retain the host's floating
npm/Git declarations. Image rebuilds can fetch new versions of those resources.

Both applications' settings remain writable. Pi's activation merge replaces
arrays instead of retaining removed package declarations, while keeping undeclared
runtime preferences. Claude's global onboarding state is initialized only when
absent; activation must not reset account state or project trust.

Pi rules, keybindings, Exa MCP configuration, presets and the Loggly agent definition
are included; `/home/tomas` paths in portable definitions become `/root`. The host's
local `extensions/herdr-agent-state.ts` is also installed unchanged through Home
Manager. It stays inactive unless all three Herdr variables (`HERDR_ENV=1`,
`HERDR_SOCKET_PATH`, `HERDR_PANE_ID`) are set; Box does not install Herdr itself or
configure host-socket forwarding. SOPS wrappers, history, runtime IDs/caches and
account-synced Claude skills are not included. Separate credentials for Exa, Loggly, Atlassian,
voice and other integrations are not copied. Those integrations were not exercised
against their services by this change.

## Authentication boundary

- `Config.secrets` defaults to `1password` and `claude-code`. The latter must contain
  `CLAUDE_CODE_OAUTH_TOKEN`. Explicit custom lists replace the defaults; include
  `claude-code` when overriding them. Saved box configurations remain authoritative
  on resume; older boxes are not silently migrated.
- `Service.up()` validates the client's `~/.pi/agent/auth.json` before creating a
  new sandbox. The `agents` setup checkpoint transfers it through exec **stdin**,
  not argv, environment, image build inputs, Git or the registry.
- The remote installer atomically publishes a regular `0600` auth file in a `0700`
  directory. Interrupted setup can retry without overwriting an already-published
  file that Pi might have refreshed. A symlink or improperly protected destination
  is an error. Completed setup/resume never rereads the original client's auth.
- Every sandbox creation/resume injects its saved named Modal secrets. Boot writes
  a private `/run/box/ssh-env.sh` for SSH/Rex shells and clears absent token values,
  then starts both network daemons without service/enrollment credentials.
- No local Claude `.credentials.json`, `oauthAccount`, machine/account IDs, project
  trust or full `.claude.json` is copied. Installer-generated machine state is
  cleared before the base image is saved.

Snapshots contain credentials. Keep them private. Pi OAuth refresh/logout can
invalidate a credential shared with the original host. Claude setup tokens are
not refreshed by this configuration: replace an expired token in Modal, then
pause/resume the box. Reattaching to a live box does not reinject a changed secret.

## Minimal Claude onboarding state

The base image seeds only:

```json
{"hasCompletedOnboarding": true}
```

This is UI state, not a credential or an authorization override. A valid
`CLAUDE_CODE_OAUTH_TOKEN` is still required. Theme, account IDs, cached organization
metadata and `lastOnboardingVersion` do not belong in the seed. New project trust
and other safety prompts are deliberately left intact.

The single-field workaround is described in Anthropic's issue reports
[#46259](https://github.com/anthropics/claude-code/issues/46259) and
[#73403](https://github.com/anthropics/claude-code/issues/73403). Consulted alongside
[authentication](https://code.claude.com/docs/en/authentication) and
[environment-variable](https://code.claude.com/docs/en/env-vars) documentation on
6 November 2026. The flag is internal application state, so retest this contract
when upgrading Claude; do not assume a print-mode invocation proves the TUI works.

## Repeatable verification

```sh
dotenvx run --quiet --strict -f .env -- \
  uv run --extra remote python scripts/smoke_agents.py
```

This creates only a uniquely named test box and checks:

1. Byte-for-byte Pi auth transfer and private file permissions; token injection
   without a Claude credential file.
2. Minimal fresh Claude state, no cloned Pi device ID, installed core versions,
   all 15 Pi packages, the exact bundled Herdr extension and both Claude plugins.
3. Credential-free network daemon environments and Home Manager reactivation,
   including array replacement and retention of runtime edits/onboarding state.
4. A fresh Claude config without the flag reaches the onboarding theme selector.
5. A separate fresh config with **only** the flag reaches an authenticated model
   reply in a real terminal, after accepting trust for the empty test workspace.
6. Configured Claude and Pi produce real interactive replies; the Pi Claude bridge
   invokes the Nix Claude binary successfully.
7. An actual SSH shell can invoke authenticated Claude without forwarding a token
   from the local SSH process.
8. Pause/resume retains Pi credentials and restores authenticated Claude access.

The terminal probe uses a private tmux server, which supplies a real PTY and
terminal emulation. It does not perform `/login`, copy Claude account metadata or
preseed project trust. The smoke check removes its box and logs out both test
network identities in `finally`; no pre-existing box or remote-exec deployment is
modified. Modal retains exit snapshots according to its normal retention policy.

Per-check evidence is saved to [agents-verification.json](agents-verification.json).
The live command must exit successfully and that file must report `success: true`;
a partially populated evidence file is not a successful run.

### Verified 6 November 2026

All **13 live checks passed**, including a Haiku reply from the minimal isolated
Claude configuration, the configured default Claude/Pi models, the Claude bridge,
actual SSH inference and a newly restored snapshot. The final test sandbox,
registry record and both network logins were removed; earlier test fixtures were
also cleaned up. Existing boxes and the deployed execution service were unchanged.

The host-local Herdr extension was added after that live run. Its Home Manager
output and inactive behavior are checked locally; desktop integration has not been
exercised, and the recorded live evidence predates that addition.

The Nix package build passed **386 tests**. Ruff and `nix flake check` passed, and
`result/bin/box` was rebuilt with the new assets. Direct host pytest still has the
nine pre-existing `/bin/bash` path failures described below.

For scripted Pi print-mode checks, explicitly redirect stdin from `/dev/null`:
Modal leaves exec stdin open, and Pi waits for piped stdin EOF even with a prompt
argument. The smoke runner enforces this; it does not mistake that wait for an
authentication failure or weaken the expected model-reply check.

Offline authentication/lifecycle tests cover missing/invalid auth, safe errors,
private atomic installation, symlink refusal, interruption recovery, no recopy on
resume, named-secret propagation, shell quoting/refresh and daemon isolation.
Use `nix build` for the complete test suite on this NixOS host: direct pytest has
nine existing Debian `/bin/bash` path failures, while the package's existing test
hook substitutes the Nix Bash path without changing the installed runtime code.
