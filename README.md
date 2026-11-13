# box

One environment, defined with Nix, for my computers and my agent sandboxes.

A Nix flake defines the tools and the user-level files. The same definition is delivered
to cloud sandboxes on Modal and Vercel Sandbox as a container image. A local computer is
only the control plane: it starts builds and sandboxes, and it does not need Docker.

`docs/decisions.md` gives the reasons for the design. `AGENTS.md` gives the rules and the
commands for work on this repository.

## Status

| Part | State |
|---|---|
| Flake with the sandbox home | Done. |
| Sandbox image built from the flake | Done. One build goes to both registries. |
| Smoke test on Modal and on Vercel Sandbox | Passes in GitHub Actions. |
| Homes for computers (NixOS, other Linux, macOS) | Not started. The layout is ready for them. |
| Automatic version updates, shared binary cache | Not started. |
| Secrets, sandbox CLI, HTTP API and MCP server | Not started. |

## How the parts fit

```
flake.lock            fixes every version
   |
home/profiles/*.nix   reusable parts: base tools, coding agents
   |
home/sandbox.nix      one home: the root user of a sandbox
   |
image/Dockerfile      Ubuntu + Nix + the built home
   |
GitHub Actions        one build -> two registries -> smoke test on each provider
```

## Layout

| Path | Content |
|---|---|
| `flake.nix`, `flake.lock` | The outputs of the flake and the exact version of every input. |
| `nix/nixpkgs-config.nix` | Settings for nixpkgs, such as the list of permitted unfree packages. |
| `home/profiles/base.nix` | Command-line tools that every home gets. |
| `home/profiles/agents.nix` | The coding agents, their manifest and their shared instruction file. |
| `home/sandbox.nix` | The home of a sandbox. `home/sandbox/AGENTS.md` is what agents read there. |
| `image/Dockerfile` | Puts the built sandbox home into an Ubuntu image. |
| `src/providers/` | One small adapter for each provider: create a sandbox, run a command, destroy. |
| `src/smoke.ts` | The smoke test. |
| `.github/workflows/image.yml` | Checks the flake, builds the image and runs the smoke test. |

## Work on the environment

You need Nix with flakes. `nix develop` adds Node.js and the linters.

| Task | Command |
|---|---|
| Check the flake and the format | `nix flake check` |
| Format the Nix files | `nix fmt` |
| Build the sandbox home | `nix build .#sandbox-home` |
| Update all versions | `nix flake update`, then commit `flake.lock` |

`AGENTS.md` says where a new tool, a new agent or a new home goes.

## What a sandbox contains

The smoke test checks each of these properties.

- The platform is `linux/amd64`. The system is Ubuntu 24.04.
- The user is root, and the working directory is `/workspace`.
- The tools of the home are on `PATH`: `claude`, `codex`, `pi`, `git`, `gh`, `mise`,
  `nix` and others.
- `~/.config/box/manifest.json` lists the coding agents and their versions.
- Each agent has the same global instruction file, `home/sandbox/AGENTS.md`.
- Nix works and reaches the public binary cache. To add a tool in a running sandbox,
  use `nix shell nixpkgs#<tool>`.
- The image has no `ENTRYPOINT` and no `CMD`. Start every process with the provider's
  "run command" call.
- The image contains no secrets. It is public.

## Run the smoke test

```sh
npm ci
node src/smoke.ts --provider modal  --image ghcr.io/<owner>/<repo>/base@sha256:<digest>
node src/smoke.ts --provider vercel --image vcr.vercel.com/<team>/<project>/base@sha256:<digest>
```

Credentials:

- Modal: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, or the active profile in `~/.modal.toml`.
- Vercel: `VERCEL_TOKEN`, `VERCEL_TEAM` (team slug or ID) and `VERCEL_PROJECT` (project name or ID).

The Vercel variables are in `.env`, encrypted with dotenvx. Put `dotenvx run --` before
the command to use them.

On Modal, `--image` also accepts a Modal image ID (`im-...`).

## The build in GitHub Actions

Vercel Sandbox pulls images only from Vercel Container Registry. Modal pulls from GitHub
Container Registry. The workflow pushes one build to both, so one digest identifies the
image on both providers.

The workflow needs these in the repository settings, under "Secrets and variables",
"Actions":

| Name | Kind | Value |
|---|---|---|
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | Secrets | A Modal token. |
| `VERCEL_TOKEN` | Secret | A Vercel access token. |
| `VERCEL_TEAM` | Variable | The slug of the team that owns the sandboxes. |
| `VERCEL_PROJECT` | Variable | The name of the project that owns the sandboxes and the image. |

The image name on Vercel is `vcr.vercel.com/<VERCEL_TEAM>/<VERCEL_PROJECT>/base`.
The Vercel steps are skipped while `VERCEL_TEAM` is not set.

GitHub creates a container package as private, and Modal then cannot pull it. The
package of this repository is already public.
