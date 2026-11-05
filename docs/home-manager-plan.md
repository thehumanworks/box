# Home Manager migration

Status: complete and verified on 2026-11-04. Installed CLI: box 0.2.0.

## Outcome

Keep Modal's Debian container and Determinate Nix. Add standalone Home Manager
as the owner of the root user's packages, shell, Git and editor configuration.
A committed flake.lock pins both nixpkgs and Home Manager. Runtime startup remains
small: refresh injected SSH credentials, start Tailscale and preserve its identity.
This is not a migration to a NixOS operating system.

## Constraints

- Keep `box up/ssh/exec/pause/resume/clone/pull` behavior and JSON contracts.
- Keep mise installed and usable, Determinate Nix as the only Nix on PATH, and
  OP_SERVICE_ACCOUNT_TOKEN usable through both exec and Tailscale SSH.
- Keep existing snapshots and boxes unchanged. Resume restores its snapshot; never
  silently reapply a new base configuration over user changes.
- Never evaluate secret values into the Nix store or generated Home Manager files.
- Preserve current package versions where feasible; do not fold in unrelated upgrades.
- Preserve effective caller Git identity without writing into Home Manager-owned
  immutable files. Separate managed Git defaults from writable identity includes.
- The workspace has no Git repository. Preserve existing files and user work.

## Proposed delivery steps

1. Record current shell, PATH, package and secret behavior. Inspect current Home
   Manager modules and match its pinned revision to our pinned nixpkgs input.
2. Create a standalone Home Manager flake and home.nix (root, /root, explicit
   stateVersion, generic Linux). Expose an activation package for each supported
   Linux architecture and include the Home Manager CLI. Use modules for Bash,
   Git, editor and mise where appropriate; packages remain in one clear list.
3. Build and activate the home during image creation. Seed writable configuration
   at /root/.config/home-manager so `home-manager build` and `home-manager switch`
   work directly. Remove the old `nix profile install` and shell-file append logic.
   Remove only known base-image shell files before the initial activation; never
   use broad force/backup overrides against later user changes.
4. Keep a minimal system bootstrap for noninteractive exec/SSH PATH and runtime
   secrets. Ensure all shell modes select the same Home Manager profile and
   Determinate Nix. Do not source a saved SSH secret before refreshing it at boot.
   Keep mutable Git author identity separate from generated config.
5. Document the source-of-truth path, edit/build/switch/rollback workflow, adding
   tools and config, lock updates, snapshot semantics and old-box behavior.
   Consider a very small `box home` command only if it removes real UX friction;
   avoid duplicating the entire Home Manager CLI.
6. Verify local tests and wheel contents, then run real Modal + Tailscale checks:
   fresh build, direct exec/non-login/login/interactive shells, Git identity,
   mise, op whoami, Nix builds, Home Manager build + repeat activation, config
   change + rollback, failure preserving old generation, and snapshot resume
   retaining both user edits and active generation. Re-run existing volume/copy
   and lifetime-expiry smoke checks. Clean only test-owned resources.
7. Ask Fable to review the integrated implementation and observed evidence.
   Resolve material findings, finish documentation, and deliver concise usage.

## Acceptance

- Home Manager owns packages and normal user configuration, discoverable in the box.
- Human and agent commands are predictable, work without systemd and report errors.
- Determinate Nix, mise and 1Password work before and after Home Manager switches
  and snapshot resumes; no token is evaluated into Nix files or the store.
- Git author identity survives configuration activation and rollback.
- A locked, packaged configuration produces a working image. Old snapshots retain
  their original behavior. Live results and limitations are recorded explicitly.

## Review decisions

Fable's complete review is in [home-manager-review.md](home-manager-review.md). Accepted corrections:

- Write inherited Git identity explicitly to `/root/.gitconfig`. Home Manager owns
  `/root/.config/git/config`; Git reads both, with mutable identity taking precedence.
- Remove exactly the base-image `.bashrc`, `.profile`, and `.bash_profile` before
  first activation; never delete or force-overwrite user files on subsequent boots.
- Put the runtime shell bridge in `programs.bash.bashrcExtra`, before the module's
  noninteractive return. Keep Debian `/bin/bash` as the SSH login shell.
- Do not install upstream Nix or let Home Manager rewrite Determinate configuration.
  Verify the resolved executable is Determinate, rather than requiring a particular
  symlink spelling: the installer already keeps Determinate in the user profile.
- Keep mise configuration mutable. Keep editor configuration user-controlled until
  the user opts into an editor module. Home Manager owns packages, Bash, Git defaults,
  session settings and mise shell integration.
- Use `homeConfigurations.root` for the current x86_64 Modal image. Direct commands
  need `USER=root` and `LOGNAME=root`; the baseline probe found these unset.
- Pin nixpkgs at `0bb2eedc20e4739f29e8045034f2828ad7054f8f` and Home Manager at
  `b787c48e2ec57a9d098f5c2721882a3bc1e16f97`, and have HM follow our nixpkgs input.
  Generate and validate the lock in an owned temporary Modal sandbox.
- Do not add a `box home` wrapper. Use `box exec NAME -- home-manager …` and explain
  the small edit/build/switch/rollback workflow clearly.
- Preserve direct-command argv while loading the user environment for `box exec`;
  Home Manager's session variables should be observable to command-line agents too.
- Never compare only secret-like prefixes in the store: test that actual runtime
  credential bytes are absent from generated Home Manager files without printing them.

Delivery checkpoints: [x] baseline and Fable review; [x] locked HM image;
[x] CLI/Git integration; [x] tests and live lifecycle proof; [x] final Fable review;
[x] package and documentation.

## Integration findings

- Fresh Determinate installation starts with a legacy `manifest.nix` profile.
  The first activation failed with `unexpected EOF reading a line`; later probes
  showed the same intermittent worker failure outside the legacy profile path. The image now adds the already-built HM
  home-path once through the modern `nix profile add` command before activation.
  This converts the profile while retaining Determinate and its CA package; later
  package changes are controlled by Home Manager. The rebuilt image activates successfully.
- `targets.genericLinux` now enables GPU checks by default. This CPU development
  image sets `targets.genericLinux.gpu.enable = false` to avoid irrelevant driver
  installation advice and dependencies.
- `git config --global --get` reads the chosen global write file, whereas normal
  `git config --get` combines the XDG defaults and ~/.gitconfig. Tests check the
  effective consumer view, and inherited identity writes target ~/.gitconfig explicitly.

- Boot no longer sources user session settings. It refreshes runtime credentials
  and starts the network daemon with a fixed system PATH, so broken Home Manager
  session hooks cannot terminate the sandbox entrypoint.
- Home Manager's generic-Linux module sources `nix.sh` after both sessionPath and
  mise's interactive hook. Restore mise precedence after those hooks for consistent
  SSH and exec resolution; retain user session paths.
- In an isolated sandbox, changed Home Manager builds failed 2/4 times with defaults
  and 3/4 with only `max-jobs = 1`. All four builds with only `eval-cores = 1` passed,
  as did four with both settings. Set only `eval-cores = 1` in the installer-owned
  custom Nix configuration; keep build parallelism. This is an observed mitigation,
  not an upstream root-cause diagnosis.
- The first complete Home Manager lifecycle run passed activation, rollback,
  shell precedence, secret isolation and snapshot preservation, then the transfer
  test correctly rejected `home-manager build`'s absolute `result` symlink. The
  smoke and recommended build commands now use native `--no-out-link`, keeping
  the workspace portable without weakening transfer validation.

## Final verification

- Fable 5.1 at high effort reviewed the plan, implementation, and corrective changes
  in the requested Rex window. All material findings are resolved; its final
  approval was conditional only on live checks, which now pass.
- `uv run pytest -q`: 107 passed plus 16 subtests. Ruff lint and formatting pass.
- Wheel and source distribution contain the exact packaged assets and locked
  configuration, without the retired package-only flake.
- `uv tool install --editable .` updates the installed CLI to 0.2.0; `box doctor`
  confirms existing Modal authentication and Tailscale connectivity.
- Image `im-Oy3kklw4V3iIIw3EIZPxXn` built successfully. The final
  `uv run python scripts/smoke.py` exited 0 with all 14 checks passing.
  See [smoke-result.json](../smoke-result.json) for exact resources and shell paths.
- Live checks cover real Tailscale SSH, Nix builds, 1Password, repeat Home Manager
  activation, package/environment changes, mise priority, Git identity, rollback,
  failed-build recovery, secret exclusion, file/volume transfers, and preservation
  of active generations and unapplied edits across manual and ungraceful exits
  and actual lifetime expiry. Shell tests cover startup; mise's prompt hook is
  retained but was not separately exercised through a typed interactive workflow.
- Test sandboxes, registry entries, Tailscale nodes and the test volume were cleaned
  up. The existing `dev` sandbox retains its original ID and environment, and the
  updated CLI was verified against it. New boxes get Home Manager; existing
  snapshots are never automatically migrated.
- No Git commit was made because this workspace is not a Git repository.

## Subsequent Rex integration check

On 2026-11-04, a later full integration run reproduced the Nix worker-startup EOF
despite `eval-cores = 1`. The setting is a mitigation, not a complete fix. Six
subsequent changed Home Manager builds passed on each of gVisor and Modal's VM
runtime using the same image and resources. That bounded comparison did not
isolate a runtime-specific cause; the existing backend is retained. Rex HTTPS
terminal execution, configured tools and 1Password authentication passed before
the unrelated Nix worker failure. See `smoke-result.json` for the latest full-suite
attempt, and `rex-smoke-result.json` for the focused Rex lifecycle check.
