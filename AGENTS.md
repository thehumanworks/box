# Instructions for agents that work on this repository

This repository defines one environment with Nix and delivers it to sandboxes on Modal
and Vercel Sandbox. Read `README.md` for the layout and `docs/decisions.md` for the
reasons behind the design.

## Rules

- The Nix flake is the only definition of tools and their versions. Do not install tools
  with `apt-get`, `curl` or `npm -g` in `image/Dockerfile`.
- The image is public. Never put a secret, a token or a personal file into `home/`,
  `nix/` or `image/`.
- A profile in `home/profiles/` must work on every system: NixOS, other Linux and macOS.
  Put what is specific to one home into that home's file, for example `home/sandbox.nix`.
- Keep each Nix file small and start it with a comment that says what it is for.
- `.env` holds values encrypted by dotenvx. It is committed on purpose. Never commit
  `.env.keys` or a decrypted value.
- The branch `legacy` holds an older, unrelated project. Do not change or delete it.

## Commands

| Task | Command |
|---|---|
| Check the flake and the format | `nix flake check` |
| Format the Nix files | `nix fmt` |
| Build the sandbox home | `nix build .#sandbox-home` |
| Update all versions | `nix flake update` |
| Get Node.js and the linters | `nix develop` |
| Type-check the TypeScript code | `npm run typecheck` |
| Lint the workflow | `actionlint` |

Run `nix flake check` and `npm run typecheck` before each commit.

## How to change the environment

- **Add a tool to every home:** add the package to `home/profiles/base.nix`.
- **Add a coding agent:** add one entry to the `agents` list in `home/profiles/agents.nix`.
  The manifest and the smoke test pick it up from there.
- **Permit an unfree package:** add its name to `nix/nixpkgs-config.nix`.
- **Add a home for a computer:** add a file in `home/` that sets the user and imports
  profiles, then add it to `homeConfigurations` in `flake.nix`.

A change to the flake, `home/`, `nix/` or `image/` builds a new image when it reaches
`main`. The workflow starts a sandbox from that image on both providers and runs
`src/smoke.ts`. Read the workflow result before you report the change as done.
