# Design decisions

Each entry gives the decision, the reasons, and what would make it worth a new look.

## 1. A Nix flake with Home Manager defines the environment

**Decision.** One flake defines the tools and the user-level files. Home Manager is the
layer that every system shares: sandboxes now, and NixOS computers, other Linux
computers and Macs later. A NixOS or macOS system configuration can import the same
profiles.

**Reasons.**

- Nix is the only tool that gives the same result on NixOS, other Linux and macOS.
- One lockfile, `flake.lock`, fixes every version. An update is one command and one
  commit, and a revert restores the exact earlier state.
- Home Manager modules are ordinary Nix modules. Other tools that build on Nix can
  reuse them.

**Alternatives.**

- *mise as the definition.* This was the first version of the image. mise downloads
  prebuilt programs that expect a usual Linux system, and these do not run on NixOS.
  mise stays in the environment for the tools that a project declares.
- *devenv as the definition (looked at for version 2.4, September 2026).* devenv
  describes the environment of one project directory. It has no environment for a whole
  user. Its "machines" feature wraps NixOS, nix-darwin and Home Manager configurations,
  but its own documentation calls it experimental, and it needs the devenv program for
  each build and deployment. The containers it builds have no Ubuntu base. Because
  devenv machines accept ordinary modules, a later move to devenv needs no rewrite of
  `home/`.

**Look again when** devenv declares machines stable, or when projects need long-running
services and tasks. devenv is a good fit for the environment of one project.

## 2. The image is Ubuntu with the home built in

**Decision.** `image/Dockerfile` builds the sandbox home with Nix and copies it into an
Ubuntu image. The image is ready to use when a sandbox starts.

**Reasons.**

- A sandbox must start fast. A home that is downloaded at each start adds a download of
  about 2.4 GiB to each new sandbox.
- An image built only from Nix has no standard program loader. Tools that mise or other
  installers download would not run in it.
- Nix is in the image, so a sandbox can still add a package or apply a newer home. Nix
  then downloads only the parts that changed.

**Look again when** start time on a provider becomes a problem, or when a provider
caches the Nix store between sandboxes.

## 3. One build goes to two registries

**Decision.** GitHub Actions builds the image once and pushes it to GitHub Container
Registry and to Vercel Container Registry. One digest identifies the image on both
providers. A smoke test then runs in a sandbox on each provider.

**Reasons.**

- Vercel Sandbox pulls images only from Vercel Container Registry.
- Both providers accept gzip layers and a plain image manifest, so one build serves both.
- The local computers have no Docker. They only start and control the builds.

**Look again when** a third provider is added, or when Modal can pull from Vercel
Container Registry with less setup than a second registry needs.

## 4. Versions follow nixos-unstable

**Decision.** `nixpkgs` follows the `nixos-unstable` branch. The coding agents come from
nixpkgs like every other tool.

**Reasons.**

- One source for all versions keeps the environment the same on every system.
- On 2026-11-13, nixos-unstable was one to three days behind the newest release of each
  coding agent. That delay is acceptable.

**Look again when** an agent release is needed on the same day. An extra flake input
that packages the agents daily can then replace the nixpkgs packages for those tools.
