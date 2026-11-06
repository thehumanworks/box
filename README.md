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
uv run --extra remote python scripts/deploy_remote.py  # once per workspace/environment
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
respect_ignores = true         # default; applies to this initial clone

[volumes]
"/mnt/data" = "my-project-data"
```

Clone paths are relative to the configuration file. `box --config path/to/box.toml up`
uses an explicit file. New boxes use its resource settings; existing boxes retain
their saved configuration. Resume skips completed clones and never copies over remote changes.
If initial setup was interrupted, it finishes only the uncompleted steps.

## Everyday commands

```sh
box ls
box status dev
box clone dev ../another-project                 # copy later
box clone dev ./fixtures /workspace/fixtures
box pull dev /workspace ./work-from-box          # bring changes home
box ssh dev --print
box --human exec dev -- npm test                 # HTTPS; buffered output
box --json exec dev -- bash -lc 'git status --short'
box exec --cwd / dev -- ls -la                   # inspect, even during incomplete setup
box exec -w /workspace/project dev -- git status # directory for this command only
box spawn dev -- python long_job.py              # background call, returns call ID
box poll CALL_ID                                # one check; exit 75 while pending
box poll --wait CALL_ID                         # wait; preserve the command's exit code
box up data --volume my-data:/mnt/data
box rm dev --yes                                # stop and forget; keep volumes
```

Uploads (`box up --clone` and `box clone`) respect Git ignore rules by default
when the source is inside a Git working tree. Git handles nested `.gitignore`
files, negation, repository-local excludes and global excludes; tracked files are
not excluded by ignore rules. Docker and npm ignore files are not used.
Outside a Git working tree, no Git ignore filtering is applied.

Use `--not-respect-ignores` to disable Git filtering or `--respect-ignores` to
enable it explicitly. On `box up`, an explicit flag overrides all initial clones
from the config and command line; otherwise each clone's `respect_ignores`
setting applies (default `true`). Existing boxes retain their saved settings.
Git must be installed on the client unless filtering is disabled.
Downloads (`box pull`) do not apply Git ignore rules.

Copies include dotfiles, Git history, executable permissions, safe relative symlinks
and empty directories. `.venv`, `.direnv`, `node_modules` and `__pycache__` are excluded
regardless of Git filtering, as are explicit `exclude` patterns. The `.direnv` cache contains machine-local state and Nix-store links;
`.envrc` and flake files are still copied. Conventional Nix build-output symlinks
named `result` or `result-*` pointing into `/nix/store` are also omitted; real files,
directories and safe relative links with those names are preserved.
Other external symlinks are rejected. Choose directories deliberately: dotfiles may contain
credentials. Copies are one-time transfers, not live synchronization. Existing
nonempty destinations require `--overwrite`, which merges and replaces matching files.
Concurrent edits during a copy do not form a consistent point-in-time snapshot.

## Remote exec over HTTPS

`box exec`, `box spawn` and `box poll` use the authenticated Modal HTTPS service.
Commands run server-side in existing boxes, avoiding this client's direct
sandbox-worker connection. Lifecycle, SSH and transfer commands remain unchanged.
Provision the service and its private proxy credentials once:

```bash
uv sync --extra remote
dotenvx run --quiet --strict -f .env -- \
  uv run --extra remote python scripts/deploy_remote.py
```

The CLI auto-detects a single provisioned deployment under
`~/.config/box/box-exec-api/` (or `$XDG_CONFIG_HOME/box/box-exec-api/`). For multiple
deployments, select `box --remote-deployment /path/to/deployment.json ...` or set
`BOX_REMOTE_DEPLOYMENT`. Selection is independent of the active Modal SDK profile.
The CLI reads the private proxy credential file; no API token or FastAPI extra is
needed for these three commands. Missing configuration is an error, not a fallback
to a direct SDK connection.

`exec --timeout` defaults to 60 seconds, with a 90-second maximum. Use spawn/poll
for longer work; `spawn --timeout` defaults to one hour. Human output is buffered,
and each stream is capped at 1 MiB. For interactive streaming, use SSH.

```sh
call_id=$(box --human spawn dev -- make test)
box --human poll --wait "$call_id"
```

The CLI generates a request ID before submission. After an uncertain request,
retry **identical input** with the `--request-id` printed in the error; do not use a
new ID. Explicit IDs are also accepted. Nothing is retried or resubmitted
silently. Proxy-token holders have shell access to managed boxes, so keep those
credentials private.

See [Remote exec on Modal](docs/remote-exec.md) for exit codes, the API contract,
credentials and repeatable CLI/HTTP smoke tests.

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
JSON execution returns `exit_code`, `stdout`, `stderr` and call/request metadata;
human mode writes buffered stdout/stderr after completion. `poll` wraps the outcome
under `result`. Pending polls exit 75, execution timeouts exit 124, and other
service/configuration failures exit 1. `poll --wait --timeout SECONDS` limits only
the local wait; it does not cancel the job.
Use `--` before remote commands to preserve their options. Normal lifecycle operations
never prompt. `box ssh` requires a terminal; agents should use `box exec`.
`box exec` uses the saved working directory. Override it for one command with
`--cwd /PATH` or `-w /PATH`, before the `--` separator. The path must be absolute
and already exist; `/` is allowed. Exec never creates directories or changes the
saved configuration. A missing directory fails before the command starts.

Names, saved configuration, sandbox IDs and locks live in a Modal Dict in the active
Modal account/environment. Another authenticated machine can `box ls` and resume a
named environment. The app is `box-dev`, the registry is `box-dev-registry-v1`.
Atomic per-name locks prevent competing create/pause/resume operations. After a CLI
crash, inspect the reported lock owner and use `box unlock NAME --force` only after
confirming that process has stopped. Lost API responses during creation may require
finding the sandbox by its `box.name` tag in Modal before removing a stale record.

## Interrupted setup

A live sandbox is not necessarily ready. `box status` reports `setup-incomplete`
for a live box whose setup did not finish, with the last setup phase in `setup`.
Default `exec` and SSH refuse an incomplete box rather than hiding the failure.
Use `box exec --cwd / NAME -- ls -la` to inspect it, then `box up NAME` to finish.
An explicit `--cwd` permits diagnostics but does not mark setup complete.

Creation, interrupted live setup and snapshot recovery use the same setup steps:
working directory, pending initial clones, saved Git identity, then networking.
Completed clones are never recopied. A pending clone needs its original source
on the client running `box up`; a missing source leaves setup incomplete. If a
copy finished but its completion was not recorded, a nonempty destination is
refused rather than overwritten or assumed complete. Inspect and preserve those
files before moving the unconfirmed destination aside and retrying.

Failed recovery leaves an existing or restored sandbox alive with its files and
setup phase retained. A failed first creation is stopped with an exit snapshot.
A failed network refresh of an already-ready live box does not erase its completed
setup checkpoint. Missing snapshots are never replaced with an empty environment.

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

## WSL, Zscaler and connection troubleshooting

`exec`, `spawn` and `poll` use normal HTTPS to `*.modal.run` with system CA trust
and TLS verification enabled. Redirects are disabled. The sandbox DNS policy below does not
affect those commands; it applies to direct SDK operations such as setup and
transfer, and to internal service-level tests.

Box uses the host's trusted CAs for Modal gRPC and adds them to Modal's blob HTTP
client. `SSL_CERT_FILE` and `SSL_CERT_DIR` are respected; certificate and hostname
verification stay enabled. On managed hosts, use the CA bundle installed by your
organization rather than disabling verification. The sandbox gRPC channel also
explicitly negotiates HTTP/2 (`h2`), including when Modal supplies its own TLS context.

Some Windows/WSL resolvers time out on `*.modal.host` even though `api.modal.com`
works. Box tries system DNS for three seconds, then uses Google's DNS-over-HTTPS
resolver for **Modal sandbox hostnames only**. The fallback is announced on stderr,
keeps the original TLS hostname/SNI, and caches answers for at most their TTL or
60 seconds. No credentials, commands or project files are sent to the DNS resolver.
Box does not change `/etc/resolv.conf`, `/etc/hosts`, Windows DNS, or proxy settings.
Configured HTTP/SOCKS proxies retain control of their own routing and DNS.
Choose the policy per process with `BOX_DNS_OVER_HTTPS`:

| Value | Direct Modal sandbox connections |
| --- | --- |
| Unset or `1` | Try system DNS first; use DoH after failure or the three-second timeout. |
| `force` | Use DoH immediately, skipping the sandbox hostname's system DNS lookup. |
| `0` | System DNS only; never use DoH or its cached answers. |

```sh
BOX_DNS_OVER_HTTPS=force ./result-with-secrets/bin/box pull dev /workspace/README.md ./README.from-box.md
```

`force` sends public sandbox hostnames to Google even if system DNS would work.
A DoH failure does not fall back to system DNS in this mode. The existing verified
HTTPS timeout and DNS cache still apply. This option only controls sandbox hostname
resolution: `api.modal.com` and `dns.google` still use their normal resolution, and
configured proxies still own their routing. It does not fix slow TLS handshakes.
Use `0` if your network policy forbids public DoH; have your administrator repair
system resolution of `*.modal.host` instead.

Before accessing the registry or acquiring a lock, box performs an authenticated
Modal API check with a 20-second deadline. Sandbox channel establishment has a
30-second deadline. These are connection deadlines, not limits on image builds,
commands or snapshot creation; `up --timeout` still controls the sandbox's lifetime.
`box doctor` distinguishes connection failures from invalid/missing credentials.
The compatibility layer targets Modal 1.6.x; test it when upgrading the SDK.

```sh
box doctor
box up dev
box exec dev -- python --version
box pause dev                   # stop compute; retain files for resume
box resume dev
box rm dev --yes                # stop and forget; retain named volumes
```

If credentials are in this repository's encrypted `.env`, use the secrets-enabled
launcher (or prefix development commands with
`dotenvx run --quiet --strict -f .env --`):

```sh
./result-with-secrets/bin/box doctor
./result-with-secrets/bin/box up dev
```

**SSH and Rex still require a working local Tailscale connection.** Modal API access
does not establish one. If `doctor` reports Tailscale unavailable, sign the host into
the appropriate tailnet and check its routing/SSH policy. `box exec` works without
local Tailscale or local Modal API authentication; it uses the selected HTTPS
service's proxy credentials. Its output is deliberately buffered by the API.
Use Tailscale SSH for interactive access and live output.

A CLI killed during earlier attempts may leave a registry lock. Confirm the reported
owner process has stopped before running `box unlock NAME --force`. Do not remove a
box just to clear a stale lock; `rm` discards its resume record.

## Development

On Nix, enter the development shell first. It creates and activates a local
`.venv` using Nix's Python; it does not install into `/nix/store`.
With direnv, `direnv allow` loads the same shell.

```sh
nix develop
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Prefer `uv sync --locked` to install the project and its development dependencies.
For pip-style dependency installation, run `uv pip install -r pyproject.toml`
inside the activated shell. Outside it, create and activate a virtual environment
first (`uv venv && source .venv/bin/activate`). Do not use `--system` or
`--break-system-packages` with Nix's immutable Python.

To run or build the standalone Nix package (no virtual environment needed):

```sh
nix run . -- --help
nix build .
./result/bin/box --help
nix flake check
```

The default Nix package runs the plain CLI. To load the bundled encrypted `.env`:

```sh
nix run .#with-secrets -- up dev
nix build .#with-secrets -o result-with-secrets
./result-with-secrets/bin/box up dev
```

The launcher uses `dotenvx run --quiet --strict`, forwarding all arguments.
dotenvx Armor handles decryption at runtime using your existing authentication;
no `.env.keys` file is required by the launcher. Armor credentials are not bundled
into the package. The development shell's `with-secrets COMMAND ...` helper uses
the same mechanism.
Existing environment variables take precedence; the launcher does not use `-o`.
Decryption errors stop execution, and dotenvx's normal injection banner is
suppressed so it does not interfere with JSON output.

The encrypted `.env` is tracked and copied into the Nix store, so the launcher
works from any directory. Changes to it require rebuilding. Store only encrypted
secret values and the public key in that file: the Nix store is not private.
Never add `.env.keys`, private keys, or plaintext secrets to the repository or
Nix expressions. dotenvx is a runtime dependency of `with-secrets`, not of the
plain CLI. Decrypted values are supplied only to the app and its children.

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
