# Review: Home Manager migration plan (`docs/home-manager-plan.md`)

Final status: all material findings below are resolved. The final live run passed
all 14 checks on 2026-11-04; see [delivery evidence](home-manager-plan.md#final-verification).
The dated reviews below preserve the reasoning and follow-up.

Reviewer: Claude Fable 5.1, read-only. Sources read: the plan, `src/box/environment.py`,
`src/box/assets/*`, `src/box/service.py` (Git identity at lines 38-46 and 180-183),
`README.md`, `tests/test_environment.py`, `tests/test_lifecycle.py`, `scripts/smoke.py`.

## Verdict

The direction is right and the plan is close to implementable. Keep Debian + Determinate Nix +
standalone Home Manager (HM); do not move to NixOS. Four corrections are required before coding.
Everything else is sequencing and DX polish.

## Architecture: Debian + Determinate + standalone HM vs NixOS

Keep the current base. Reasons, all grounded in how the box works today:

- Modal's exec path and Tailscale SSH both depend on an FHS layout: `/bin/bash` as root's login
  shell (`install.sh:35`), `/usr/local/bin/python` for `network.py` and `ssh_env.py`, Debian
  `/etc/profile` sourcing `/etc/profile.d/box.sh`. A NixOS image has none of these without
  activation, and NixOS activation assumes systemd, which Modal sandboxes do not run.
- Noninteractive SSH commands work today only because Debian's bash is built with
  `SSH_SOURCE_BASHRC` (bash sources `~/.bashrc` when `SSH_CLIENT` is set). nixpkgs bash does not
  do this. This is a hidden load-bearing fact; record it in step 1.
- HM already covers the actual goal (packages, shell, Git, editor for one user). NixOS adds
  ownership of `/etc`, users and init with no benefit in a single-root container.

## Required corrections

1. **Git identity write path will break HM-managed Git config.** `programs.git` writes
   `~/.config/git/config` as a read-only store symlink. `git config --global` writes to
   `~/.gitconfig` only if it exists; otherwise it writes to the XDG file. Git writes via
   `config.lock` + rename, which replaces the HM symlink with a plain file. The box would then
   fail its next `home-manager switch` on the link-target check. Fix: change `service.py:181` to
   `git config --file /root/.gitconfig key value`. Git reads both global files and `~/.gitconfig`
   has precedence, so no HM `includes` stanza is needed. Keep HM free of `userName`/`userEmail`.
2. **Remove `/root/.bash_profile`, not just `.bashrc`/`.profile`.** `install.sh:30-33` creates
   it. Bash reads `.bash_profile` first and stops, so a leftover copy shadows HM's generated
   `~/.profile` in every `bash -l` and `box ssh` session. The pre-activation cleanup list is
   exactly: `/root/.bashrc`, `/root/.profile`, `/root/.bash_profile`. Nothing else, no `-b`.
3. **`. /etc/profile.d/box.sh` must go in `programs.bash.bashrcExtra`, not `initExtra`.** HM's
   `.bashrc` returns early for noninteractive shells after `bashrcExtra`. SSH noninteractive
   commands rely on this line running (see architecture note). `eval "$(mise activate bash)"`
   belongs in `initExtra` (or `programs.mise.enable`), matching today's interactive guard.
4. **Keep Determinate Nix as the only `nix`.** `/root/.nix-profile/bin` precedes
   `/nix/var/nix/profiles/default/bin` in `PATH`. Do not put `pkgs.nix` in `home.packages` and do
   not set any `nix.*` HM options (they would also generate a user `nix.conf`). Add a check that
   `command -v nix` resolves under `/nix/var/nix/profiles/default/bin`.

Also required, already implied by the plan but worth making explicit:

- Drop `nix profile install` entirely. HM installs `home-manager-path` into the same per-user
  profile and will report collisions with the existing `box-development` buildEnv.
- `tailscale` must remain in `home.packages`; `network.py:12` hardcodes
  `/root/.nix-profile/bin/tailscale`.
- Resume never runs activation. `boot.sh` stays as is. Old snapshots keep the old layout and
  simply lack `home-manager`; say so in the README.

## Activation, profiles, collisions

- Standalone HM without systemd works. Avoid every `services.*` option; `systemd.user` has nothing
  to talk to.
- HM builds the generation, then checks link targets, then links. A failed check leaves the old
  generation active, so the "failure preserves old generation" acceptance item is cheap to test.
- Generations live under `/root/.local/state/nix/profiles/` and the store under `/nix`. Both are
  in the exit snapshot, so resume keeps the active generation and rollback history.
- Repeat `home-manager switch` with no change is a no-op by design. Test it anyway on Determinate
  Nix because HM drives `nix profile remove/install` and profile manifest formats have shifted.
- Do not let HM own files the user edits by hand (editor `init.lua`, `~/.config/mise/config.toml`).
  HM owns packages, shell files and Git defaults. Settle this boundary before coding.

## Source of truth and locking

- Seed `/root/.config/home-manager/{flake.nix,home.nix,flake.lock}` as plain copies, not store
  symlinks. Put them under `src/box/assets/home/` so the copy is `cp -r /opt/box/home ...` and
  does not drag `boot.sh` along. hatchling ships non-Python files in the package directory, so the
  wheel-content test in step 6 covers `flake.lock`.
- Set `inputs.home-manager.inputs.nixpkgs.follows = "nixpkgs"` or the image fetches two nixpkgs.
- Pick an HM revision from the same window as nixpkgs `0bb2eedc` so the `stateVersion` check and
  module options line up. Record the pair in step 1.
- Generating `flake.lock` needs Nix. If the workstation lacks it, create the lock in a throwaway
  box and `box pull` it. Do not keep `--no-write-lock-file` once the lock is committed.
- HM evaluates the flake purely. `/run/box/ssh-env.sh` cannot be read at eval time, and image
  build runs without Modal secrets. Add a unit test that no `.nix` under assets mentions
  `OP_SERVICE_ACCOUNT_TOKEN` or `TAILSCALE`, plus a live grep of the activation closure for
  `ops_` to prove the store is clean.
- The HM flake directory is copied into the store on every evaluation. Tell users not to park large
  files there.

## Shell startup matrix (what must keep working)

| Mode | Startup files | Where the box hook lives |
| --- | --- | --- |
| `sb.exec cmd` | none; image env (`PATH`, `BASH_ENV`) | `environment.py` env |
| `box exec -- bash -lc` | `/etc/profile` → `~/.profile` → `~/.bashrc` | `profile.d/box.sh`, `bashrcExtra` |
| `ssh host cmd` (Tailscale) | `~/.bashrc` via `SSH_SOURCE_BASHRC` | `bashrcExtra` |
| `box ssh` (`bash -l`, tty) | `/etc/profile` → `~/.profile` → `~/.bashrc` | both, plus `initExtra` |

Keep `ENTRYPOINT` with `env -u BASH_ENV` and the boot-order test; they are unaffected.

## Architecture naming (settle before coding)

HM's CLI picks `homeConfigurations.$USER` when `--flake dir` has no fragment. Modal sandboxes are
x86_64 today. Either define a single `homeConfigurations.root` for x86_64 so `home-manager switch`
works with no fragment, or accept that users must type `#root-x86_64-linux`. Recommend the first;
keep the aarch64 branch in `install.sh` only if Modal confirms arm hosts.

## Ordered implementation and test sequence

1. **Record baseline and pins.** Capture `command -v` output for nix, mise, git, op, tailscale
   in all four shell modes, plus `git config --show-origin --list` and the nixpkgs/HM revision
   pair. No code change.
2. **Add `assets/home/`** with `flake.nix`, `home.nix`, `flake.lock`. Unit tests: lock present,
   no secret names in `.nix`, wheel contains the files. `home.nix` starts with a five-line comment:
   this is your file, edit it, run `home-manager switch`, roll back with `home-manager generations`.
3. **Rewrite `install.sh`:** keep installer; delete the three shell files; copy `assets/home`;
   build the activation package and run `./activate`; remove `nix profile install` and bashrc
   edits; keep `usermod`, `/var/lib/box/tailscale`. Keep `shell.sh` unchanged.
4. **Change `service.py:181`** to `--file /root/.gitconfig`. Update `tests/test_lifecycle.py`
   around line 130 to assert the new argv.
5. **Docs.** README: source-of-truth path, the three commands, lock update, "boxes created
   before this version have no Home Manager." Skip `box home`; `box exec dev -- home-manager
   switch` already exists.
6. **Extend `scripts/smoke.py`** rather than adding a new harness: Determinate `nix` path check;
   `home-manager switch` no-op; `sed` a package into `home.nix`, switch, confirm; roll back one
   generation, confirm; break `home.nix`, confirm switch fails and old generation stays; identity
   survives switch and rollback; closure grep for `ops_`; pause, resume, confirm active generation
   and the edited `home.nix` persist. Existing volume, copy and expiry checks stay.
7. **Final review** of implementation and smoke evidence.

## Optional enhancements

- `box doctor` could report whether the box's image predates HM, using the registry record.
- `programs.mise.enable` instead of a hand-written `eval`, if the module output matches today.
- A `box exec dev -- home-manager news` mention in docs for lock bumps.

## Must be settled before coding

1. Git identity path: `--file /root/.gitconfig` (recommended) vs HM `includes`.
2. HM ownership boundary: packages, bash, git defaults only; editor and mise configs stay writable.
3. Single `homeConfigurations.root` for x86_64 vs per-architecture names.
4. Where `flake.lock` is generated the first time (local Nix or throwaway box).


# Implementation review and resolution

The live smoke run is no longer running and failed at box creation. Here is the final report.

# Implementation review: Home Manager migration

## Status

Not approvable yet. The code matches the agreed design and the unit-level contracts are sound, but the only live evidence is a failed run. `/tmp/box-hm-live-smoke.log` and `smoke-result.json` (both 18:14) show the smoke aborted inside `service.up` with `ConflictError: Modal Sandbox is shutting down` during the first `copy_to` exec, right after the `mkdir` exec succeeded. No checks passed. No `smoke.py` process is running now. The new image has therefore never been observed completing a boot.

## Material issues

1. **Boot now depends on user-mutable Home Manager output.** `src/box/assets/boot.sh:7` sources `/etc/profile.d/box.sh` under `set -euo pipefail`, and `src/box/assets/shell.sh:9-14` now sources the active generation's `hm-session-vars.sh`, which in turn sources nixpkgs' `nix.sh` and whatever `home.sessionVariables` the user adds. Any nonzero status or unbound variable there kills the entrypoint, which shuts the sandbox down before SSH exists, with no way for the user to recover except a new box. This is the most likely class of cause for the smoke failure, though I could not confirm the specific trigger: I checked the pinned HM `home-environment.nix`, `lib/shell.nix`, `xdg/system-dirs.nix` and Nix's `nix-profile.sh.in`, and all variable references are guarded given `HOME`, `USER`, `PATH` and `TERM` set. The other candidate is `tailscaled` exiting early, which `wait` propagates through `set -e`. Required: make boot independent of HM. Either give `boot.sh` its own static `PATH` export and stop sourcing `box.sh`, or wrap the source in `set +eu; . /etc/profile.d/box.sh; set -eu`. Then capture the failed sandbox's output with `modal sandbox logs` (find it by tag `box.name=box-smoke-7c1a5ba8`) before rerunning, so the real trigger is recorded.

2. **PATH order differs between shell modes.** `nix.sh` prepends `/root/.nix-profile/bin` and is guarded by its own `__ETC_PROFILE_NIX_SOURCED`, which `shell.sh` does not reset. A plain Tailscale `ssh host cmd` runs `box.sh` once, so nix-profile binaries precede mise shims. `box exec` and interactive shells run `box.sh` twice, so shims come first. A `mise use -g python@…` then resolves differently over raw SSH than over `box exec`. Fix in `shell.sh`: after the HM block, re-assert the shim prefix only when it is not already first, or move the canonical PATH export after the HM block and accept that `home.sessionPath` is then appended by the user, not prepended. Verify with `printenv PATH` in all four modes.

3. **Home Manager packages and Determinate Nix share one profile.** `install.sh:29` converts `/nix/var/nix/profiles/default` (root's `~/.nix-profile`) to the modern manifest, so every `home-manager switch` writes a new generation of the profile that also holds `nix` and the CA bundle. Functionally fine, and the smoke asserts Determinate stays on `PATH`. Add one README sentence: do not run `nix profile remove --all`, `nix profile wipe-history` or `nix profile upgrade` in the box, because that profile also owns Nix itself; manage packages through `home.nix`.

## Verified acceptable

- **Active generation and source ownership.** Seeded files are copies, HM-managed files are symlinks, boot never reactivates, rollback is via `home-manager switch --rollback`, which I confirmed exists in the pinned CLI. The CLI falls back to `homeConfigurations.root` and requires `USER`, which the image env and Tailscale SSH both set.
- **Secrets.** Flake evaluation is pure, the image build has no Modal secrets, `shell.sh` sources `ssh-env.sh` after HM vars so the boot-refreshed token wins, and the smoke greps generated files for the token without printing it.
- **`box exec` argv.** `service.py:282-290` passes user arguments only as positional parameters. The local test exercises quoting, injection and empty strings. Exit codes and `workdir` are preserved.
- **Git identity.** `--file /root/.gitconfig` plus `touch` in `install.sh:24` keeps identity writable and ahead of the HM XDG config in precedence. Unit test covers the symlink surviving.
- **Existing snapshots.** Old images contain the old `box.sh` and `/bin/bash`, so the new `execute` wrapper works on them. Identity writes only happen at creation. Boot assets come from the snapshot, so old boxes are untouched.

## Remaining verification gaps

- A full green `scripts/smoke.py` run on the corrected image, with the boot log of the failed sandbox attached to the evidence.
- PATH order printed for `box exec`, `ssh host cmd`, `bash -lic` and `box ssh`.
- `home-manager switch` output inspected once for systemd or `stateVersion` warnings so the docs can mention them if present.


# Fable follow-up after fixes

## Final assessment of the three fixes

**Verdict:** All three findings are resolved correctly. No remaining consequential issue in the reviewed code. Live evidence is pending.

**1. Boot isolation (resolved).** `boot.sh` no longer sources `box.sh` or any Home Manager output. It refreshes `ssh-env.sh`, sets a system-only `PATH`, unsets the service token, and launches `/root/.nix-profile/bin/tailscaled` by absolute path. The regression test in `tests/test_home_manager.py:127-153` proves a failing profile is never sourced and the daemon sees no token. The confirmed trigger, an unguarded `XCURSOR_PATH` in the generated session file, is exactly the class the earlier review warned about, and the fix removes the class rather than the instance. The scoped `set +u` in `shell.sh:15-18` preserves the caller's option, verified by the test at line 103.

**2. PATH priority (resolved).** `shell.sh:22-25` re-asserts the mise prefix only when it is not already first, so nested shells do not grow `PATH`. `home.nix:46-48` uses `lib.mkAfter` to re-prepend after the interactive nix.sh hook. The smoke `verify_shell_paths` checks the prefix, a managed variable and shim resolution across `box exec`, SSH command, interactive and login modes, before and after a switch. One non-blocking note: in a real interactive session, `mise activate` rewrites `PATH` at each prompt and strips the shims directory by design, so interactive priority is upheld by mise's own activation rather than by the shims prefix. The `bash -ic` probe does not exercise `PROMPT_COMMAND`, so it proves startup order, not steady-state. That is acceptable and matches mise's documented behavior.

**3. Shared profile warning (resolved).** README lines 125-126 tell users to manage packages through `home.nix` and avoid removing all entries or bulk-upgrading the profile that also holds Determinate Nix.

**Evaluation mitigation.** `eval-cores = 1` in `install.sh:14` is a reasonable measured mitigation. It slows evaluation of large flakes but leaves builds parallel, and the failure mode it avoids would otherwise make `home-manager switch` flaky for users. Keep the comment honest as it is now, since the root cause is not diagnosed.

**Live evidence.** Pending. The current run created `box-smoke-10c7ae1d` at 18:30:42 and is still executing with no traceback in the log. Boot of the corrected image is therefore confirmed working, since the box reached running status with Tailscale up. The intermediate `smoke-result.json` written at 18:26:46 reached four checks and then failed. That is the run that isolated the parallel-evaluation problem, not evidence against the current code. Treat the implementation as approved contingent on this run finishing green and the result file's timestamp postdating 18:32.


## Coordinator verification after Fable's final assessment

The final smoke run (`box-smoke-7c94111a`, completed 2026-11-04 at approximately
18:40 UTC) passed all 14 checks and exited 0. This satisfies Fable's remaining
live-verification condition. The smoke uses Home Manager's native `--no-out-link`
so its build does not leave a nonportable Nix-store symlink in the transfer fixture.
Test resources were cleaned up; existing `dev` was preserved. Exact results are
in [smoke-result.json](../smoke-result.json).
