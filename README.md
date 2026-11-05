# box

Portable development environments on Modal, with Determinate Nix, Home Manager,
mise, Rex and Tailscale SSH.
`box up` creates or reconnects. `box pause` stops compute and saves the filesystem.
`box resume` restores it, including installed packages and uncommitted work.

## Quick start

```sh
uv tool install .
uvx modal token new
box doctor
box up dev --clone .
box ssh dev
box exec dev -- python --version
box pause dev
box resume dev
```

Your local machine must be signed into Tailscale. Create a Modal secret named
`tailscale` containing `TAILSCALE_AUTHKEY`, or provide `TAILSCALE_AUTH_KEY` to `box up`.
Use a reusable, preauthorized, untagged Tailscale auth key owned by your user.
Each box enrolls two devices: one for SSH and Rex's own embedded Tailscale node.
Configure your tailnet's SSH policy to allow your user to connect to the box as `root`.
Keys are supplied only at runtime; they are not written to project configuration
or the base image. Both saved Tailscale identities are reused on resume.

The 1Password CLI (`op`) is installed. Every box receives the existing `1password`
Modal secret with `OP_SERVICE_ACCOUNT_TOKEN`, including after resume. Run
`box exec dev -- op vault list` to use the service account without interactive login.
SSH shells receive this token through a root-only runtime file, replaced from the
current Modal secret at every boot. Filesystem snapshots can contain that file;
treat snapshots as sensitive and rotate/revoke the service account token as needed.

`box up dev --clone .` copies this directory to `/workspace/<directory-name>`.
Use `box.toml` to put the project directly in the shell's working directory:

```toml
name = "dev"
workdir = "/workspace"
cpu = 2
memory = 4096
timeout = 86400
tailscale_secret = "tailscale"
secrets = ["1password"]         # default; add other Modal secret names here
# region = "eu-west"

[[clones]]
source = "."
destination = "/workspace"
exclude = ["dist", ".env"]

[volumes]
"/mnt/data" = "my-project-data"
```

Clone paths are relative to the configuration file. `box --config path/to/box.toml up`
uses an explicit file. New boxes use its resource settings; existing boxes retain
their saved configuration. Resume never recopies local files over remote changes.

## Everyday commands

```sh
box ls
box status dev
box clone dev ../another-project                 # copy later
box clone dev ./fixtures /workspace/fixtures
box pull dev /workspace ./work-from-box          # bring changes home
box ssh dev --print
box --human exec dev -- npm test                 # stream output
box --json exec dev -- bash -lc 'git status --short'
box up data --volume my-data:/mnt/data
box rm dev --yes                                # stop and forget; keep volumes
```

Copies include dotfiles, Git history, executable permissions, safe relative symlinks
and empty directories. `.venv`, `node_modules` and `__pycache__` are excluded by default.
External symlinks are rejected. Choose directories deliberately: dotfiles may contain
credentials. Copies are one-time transfers, not live synchronization. Existing
nonempty destinations require `--overwrite`, which merges and replaces matching files.
Concurrent edits during a copy do not form a consistent point-in-time snapshot.

## Rex remote access

New boxes start Rex automatically. `box up`, `box resume` and `box status` show its
HTTPS address alongside SSH; JSON includes `rex_url`, `rex_hostname` and
`rex_host_id`. In the Rex app, choose **Add Remote Host…** and enter that address.
From a machine signed into your Tailscale account:

```sh
box --human status dev
rex -S "$(box --json status dev | jq -r .rex_url)" ls
```

Use the exact address printed by box. Rex joins the tailnet separately from the
SSH service, using its built-in Tailscale support. Only the Tailscale user who
owns that Rex node can connect; other tailnet members are not automatically
admitted. Your tailnet needs MagicDNS and HTTPS certificates enabled. The same
runtime auth key enrolls both nodes without a browser login or a saved key file.

Rex terminals load the same Home Manager environment, mise integration and
1Password runtime credentials as SSH shells. Rex's configuration stays editable
at `~/.config/rex/init.lua`; its state and Tailscale identity live under
`~/.local/state/rex`. Pause/resume retains that state, but processes restart.
The box supervises Rex and SSH together, so if either daemon exits, the box stops
and can be resumed. Rex diagnostics are in `~/.local/state/rex/server.log`.

Rex is pinned in the Home Manager flake and installed with its terminfo entries
and SSH terminal fallback. To update Rex, select its version in `inputs.rex.url`
and run `nix flake update rex`, then build and switch the Home Manager configuration.
Boxes created before this addition retain their old
images on resume; create a new box to get Rex automatically. See the official
[Linux installation](https://www.superlogical.com/rex/docs/linux/installation) and
[remote access](https://www.superlogical.com/rex/docs/linux/remote-access) guides.

## Your environment configuration

Each new box runs Debian with Determinate Nix and **standalone Home Manager**.
The remote configuration lives in `/root/.config/home-manager/`:

- `home.nix`: packages, Bash settings, Git defaults, session variables and mise integration.
- `flake.nix`: the configuration entry point and dependency revisions.
- `flake.lock`: exact Home Manager and nixpkgs inputs, including content hashes.

These are ordinary writable files. Edit `home.nix` to change your environment:

```sh
box ssh dev
nvim ~/.config/home-manager/home.nix
home-manager build --no-out-link    # validate and build without activating
home-manager switch                 # apply; new SSH/exec sessions use the result
home-manager generations             # inspect previous configurations
home-manager switch --rollback      # restore the previous active generation
```

For an agent, the same commands work through `box exec`:

```sh
box --human exec dev -- home-manager build --no-out-link
box --human exec dev -- home-manager switch
box exec dev -- home-manager generations
box exec dev -- home-manager switch --rollback
```

Add tools to `home.packages` and settings to the corresponding Home Manager module.
For example, add `hello` to the package list or `MY_PROJECT = "example";` inside
`home.sessionVariables`. `box exec` loads the active configuration while preserving
command arguments literally. Open a fresh SSH session after switching to refresh
interactive shell settings.
`--no-out-link` avoids leaving a `result` symlink to the remote Nix store in your
project, where it cannot be included in a portable `box pull`.

Home Manager generates `.bashrc`, `.profile`, `.bash_profile` and
`~/.config/git/config`; edit their definitions in `home.nix`, not the generated files.
Git author name/email are inherited at first creation into writable `~/.gitconfig`;
credential helpers are not copied. `git config --global` remains usable for local
overrides. The main mise config and editor configuration remain writable, so
`mise use --global` and ordinary editor setup work without fighting generated files.
Mise shims take precedence over the default tools in both SSH and `box exec`.
Manage packages through `home.nix`: the shared Nix profile also contains
Determinate Nix itself, so avoid removing all entries or bulk-upgrading that profile.

Rollback changes the active generation, not your editable source files. Keep your
configuration in version control if you also want source history. Pause/resume
preserves both active generations and unapplied edits; boot never automatically
runs `home-manager switch`. Boxes created before Home Manager was added keep their
original snapshot and do not gain it on resume. Create a new box and copy your
project/configuration when you want the new base.

The dependency URLs are pinned to specific revisions. To upgrade, choose compatible
nixpkgs and Home Manager revisions in `flake.nix`, then run `nix flake update` from
that directory, `home-manager build`, and `home-manager switch`. Keep
`home.stateVersion` unchanged unless deliberately migrating its compatibility defaults.
Do not keep secrets or large project directories alongside the flake: Nix copies
the configuration directory into its store. Runtime credentials belong in Modal secrets.

Use a project `flake.nix` + `flake.lock` and `nix develop` for project-specific tools;
mise is available for existing `mise.toml` workflows. Install and authenticate your
preferred coding agents inside the box, or supply named Modal secrets explicitly.
The current Modal image targets x86_64 Linux; the client can run on other host architectures.
Nix evaluation uses one thread because parallel evaluation intermittently failed
to start build workers in this container runtime. Builds can still run in parallel.
An intermittent `unexpected EOF reading a line` worker failure has also occurred
with this setting; the active Home Manager generation stays unchanged on a failed build.

## Humans and agents

Terminal output is concise. Redirected output defaults to JSON; `--json` and `--human`
override it and go before the command. `box exec` returns the remote exit code.
Image builds stream progress to stdout in human mode and stderr in JSON mode.
JSON execution returns `exit_code`, `stdout` and `stderr`; human mode streams both.
Use `--` before remote commands to preserve their options. Normal lifecycle operations
never prompt. `box ssh` requires a terminal; agents should use `box exec`.

Names, saved configuration, sandbox IDs and locks live in a Modal Dict in the active
Modal account/environment. Another authenticated machine can `box ls` and resume a
named environment. The app is `box-dev`, the registry is `box-dev-registry-v1`.
Atomic per-name locks prevent competing create/pause/resume operations. After a CLI
crash, inspect the reported lock owner and use `box unlock NAME --force` only after
confirming that process has stopped. Lost API responses during creation may require
finding the sandbox by its `box.name` tag in Modal before removing a stale record.

## Persistence

Every created and resumed sandbox enables Modal's experimental exit snapshots.
Pause waits for the snapshot; automatic lifetime/idle exits are recovered on the next
resume. A missing, failed or expired snapshot is an error; box never substitutes a
fresh filesystem. Snapshot retrieval waits up to five minutes and is safe to retry.

Exit snapshots preserve files, not running processes or mounted volumes. They expire
after 30 days and do not guarantee durability on rare host failures. Modal Dict
entries also expire after 30 days without access. Keep long-term work in Git or
named Volume v2 mounts, which have a separate lifecycle and are retained on removal.
`box pause` is not a backup. See [Modal's exit snapshot contract](https://modal.com/docs/guide/sandbox-exit-snapshots)
and [Dict retention](https://modal.com/docs/guide/dicts).

Default lifetime is 24 hours. Idle timeout is disabled by default because Tailscale
SSH activity may not reset Modal's idle timer. An optional `idle_timeout` in seconds
or `--idle-timeout` enables it. Closing SSH does not pause a box; run `box pause` to
stop compute. Resume restarts the network daemon using its saved identity.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Run the opt-in live check with `uv run python scripts/smoke.py`. It verifies actual
SSH, 1Password authentication, Nix builds, copies, mounted storage, snapshot resume
and lifetime expiry, then stops its uniquely named boxes and deletes its test volumes.
Results are written to `smoke-result.json`.
The check also exercises Home Manager builds, repeated activation, package and
environment changes, rollback, failed-build recovery, and snapshot preservation
of the active generation and unapplied configuration edits.
It also connects to Rex over HTTPS, runs a real remote terminal with the configured
tools and 1Password, checks Rex identity across resumes, and logs out both test
devices during cleanup.

For focused Rex verification without Nix rebuilds, run
`uv run python scripts/smoke_rex.py`. It checks remote terminals, daemon credential
isolation, saved identities, pause/resume and lifetime expiry. Results are written
to `rex-smoke-result.json`; its test box and both network logins are cleaned up.
