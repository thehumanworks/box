# Environment

You are in a sandbox: a disposable Linux container. You are the root user.

- The system is Ubuntu. The tools come from Nix and are on `PATH`.
- `~/.config/box/manifest.json` lists the installed coding agents and their versions.
- To use a tool that is not installed, run `nix shell nixpkgs#<tool>`, or use `mise` if the
  project has a `mise.toml`.
- Do not use `apt-get install` for tools. The result is lost with the sandbox, and other
  sandboxes do not get it.
- The sandbox has no secrets unless the person who started it passed them in.
- Work in `/workspace` unless you are told otherwise.
