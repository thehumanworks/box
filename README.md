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
| `image/Dockerfile` | The base image: Ubuntu 24.04, a C toolchain, mise, and the tools. |
| `image/mise.toml` | The tools, the environment and the dotfiles. It becomes `/etc/mise/config.toml`. |
| `image/mise.lock` | The exact versions, download addresses and checksums of the tools. Generated. |
| `image/dotfiles/` | Default config files that the build copies into `/root`. |
| `src/providers/` | One small adapter for each provider: create a sandbox, run a command, destroy. |
| `src/smoke.ts` | The smoke test. |
| `.github/workflows/image.yml` | Builds the image, pushes it, and runs the smoke test on each provider. |

## The image contract

Later work can rely on these properties. The smoke test checks each of them.

- The platform is `linux/amd64`.
- The user is root, and the working directory is `/workspace`.
- `mise`, `git`, a C compiler and every tool in `image/mise.toml` are on `PATH`: the
  language runtimes (Node.js, Bun, Python, Rust, Perl, Ruby), the agents (`claude`,
  `codex`, `pi`), and the secret and developer tools.
- The tools are at the versions in `image/mise.lock`. Claude Code does not update itself.
- The tools also run when a provider drops the image environment, because each tool
  is linked into `/usr/local/bin`.
- `/root` has the default config files: `~/.claude.json` (Claude Code starts without the
  onboarding) and `~/.config/fnox/config.toml` (the secrets that fnox reads from Doppler).
- An interactive bash shell activates mise and fnox. The variables in `[env]` of
  `image/mise.toml` apply there, and to each tool that starts through its mise shim.
  fnox loads the secrets only if the sandbox has a Doppler token (`DOPPLER_TOKEN`).
- The image has no `ENTRYPOINT` and no `CMD`. Start every process with the provider's
  "run command" call.
- The image contains no secrets. It is public. `image/dotfiles/fnox.toml` has only the
  names of the secrets and of the Doppler project.

To add a tool in a running sandbox, use mise: `mise use -g node@24`.

## Add or update a tool

The tools in `image/mise.toml` follow `latest`. The lockfile fixes the versions, so the
same commit always builds the same tools.

1. To add a tool, add it to `image/mise.toml`, and add its program to `PROBES` in
   `src/smoke.ts`. To update all tools to their newest releases, change nothing.
2. Run `mise run image:lock`. The first time, run `mise trust image/mise.toml` before.
3. Commit the changed files. The build installs only what the lockfile contains.

`mise lock` downloads some releases to record their checksums, so the network must permit
each download site. Tailscale is not in the image for this reason: see the note in
`image/mise.toml`.

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

Each build has the commit SHA as its tag. On `main`, a build that passes the smoke tests
also gets the tag `latest` on GitHub Container Registry: `ghcr.io/<owner>/<repo>/base:latest`.

GitHub creates the container package as private, and Modal then cannot pull it. The
package of this repository is already public. In a new repository, open the package
settings on GitHub after the first build and set the visibility to public.
