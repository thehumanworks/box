# box

Agent sandboxes on Modal and Vercel Sandbox from one base image.

The goal is that a person or an agent can start a sandbox on any supported provider and
find the same tools in it. A local machine is only the control plane.

## Status

| Part | State |
|---|---|
| Base image definition (`image/`) | Done. |
| Build and push in GitHub Actions | Done. One build goes to both registries. |
| Smoke test on Modal | Passes in GitHub Actions. |
| Smoke test on Vercel Sandbox | Passes in GitHub Actions. |
| Secrets, sandbox CLI, HTTP API and MCP server | Not started. |

## Layout

| Path | Content |
|---|---|
| `image/Dockerfile` | The base image: Ubuntu 24.04, mise, and the agent tools. |
| `image/mise.toml` | The agent tools and their exact versions. It becomes `/etc/mise/config.toml`. |
| `image/mise.lock` | Download addresses and checksums for those versions. Generated. |
| `src/providers/` | One small adapter for each provider: create a sandbox, run a command, destroy. |
| `src/smoke.ts` | The smoke test. |
| `.github/workflows/image.yml` | Builds the image, pushes it, and runs the smoke test on each provider. |

## The image contract

Later work can rely on these properties. The smoke test checks each of them.

- The platform is `linux/amd64`.
- The user is root, and the working directory is `/workspace`.
- `mise`, `claude`, `codex`, `pi` and `git` are on `PATH`.
- The agent tools are at the versions in `image/mise.toml`. Claude Code does not update itself.
- The agent tools also run when a provider drops the image environment, because each tool
  is linked into `/usr/local/bin`.
- The image has no `ENTRYPOINT` and no `CMD`. Start every process with the provider's
  "run command" call.
- The image contains no secrets. It is public.

To add a tool in a running sandbox, use mise: `mise use -g node@24`.

## Change a tool version

1. Edit the version in `image/mise.toml`. Use an exact version.
2. Run `mise run image:lock`. The first time, run `mise trust image/mise.toml` before.
3. Commit both files. The build installs only what the lockfile contains.

To change the mise version, edit `MISE_VERSION` and `MISE_SHA256` in `image/Dockerfile`.
The checksum is in `SHASUMS256.txt` of the mise release.

## Run the smoke test

```sh
npm ci
node src/smoke.ts --provider modal  --image ghcr.io/<owner>/<repo>/base@sha256:<digest>
node src/smoke.ts --provider vercel --image vcr.vercel.com/<team>/<project>/base@sha256:<digest>
```

Credentials:

- Modal: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, or the active profile in `~/.modal.toml`.
- Vercel: `VERCEL_TOKEN`, `VERCEL_TEAM` (team slug or ID) and `VERCEL_PROJECT` (project name or ID).

If the variables are in a `.env` file that dotenvx encrypted, put `dotenvx run --` before
the command.

On Modal, `--image` also accepts a Modal image ID (`im-...`). This permits a test of an
image that Modal built from the Dockerfile, without a registry.

## Set up the build in GitHub Actions

Vercel Sandbox pulls images only from Vercel Container Registry. Modal pulls from GitHub
Container Registry. The workflow pushes one build to both, so one digest identifies the
image on both providers.

Add these in the repository settings, under "Secrets and variables", "Actions":

| Name | Kind | Value |
|---|---|---|
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | Secrets | A Modal token. |
| `VERCEL_TOKEN` | Secret | A Vercel access token. |
| `VERCEL_TEAM` | Variable | The slug of the team that owns the sandboxes. |
| `VERCEL_PROJECT` | Variable | The name of the project that owns the sandboxes and the image. |

The image name on Vercel is `vcr.vercel.com/<VERCEL_TEAM>/<VERCEL_PROJECT>/base`.
The Vercel steps are skipped while `VERCEL_TEAM` is not set.

GitHub creates the container package as private, and Modal then cannot pull it. The
package of this repository is already public. In a new repository, open the package
settings on GitHub after the first build and set the visibility to public.
