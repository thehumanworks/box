# The coding agents, and the files that they all share.
#
# To add an agent, add one entry to `agents`. If its global instruction file has a
# different location, add that location to `instructionFiles`.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.box.agents;

  agents = [
    {
      command = "claude";
      package = pkgs.claude-code;
    }
    {
      command = "codex";
      package = pkgs.codex;
    }
    {
      command = "pi";
      package = pkgs.pi-coding-agent;
    }
  ];

  # Where each agent reads its global instructions, relative to the home directory.
  instructionFiles = [
    ".claude/CLAUDE.md"
    ".codex/AGENTS.md"
    ".pi/agent/AGENTS.md"
  ];
in
{
  options.box.agents.instructions = lib.mkOption {
    type = lib.types.nullOr lib.types.path;
    default = null;
    description = ''
      A Markdown file with instructions for all agents. It is installed as the global
      instruction file of each agent, so one file serves all of them.
    '';
  };

  config = {
    home.packages = map (agent: agent.package) agents;

    # A record of the installed agents. People, agents and the smoke test read it to
    # learn what this home contains.
    xdg.configFile."box/manifest.json".text = builtins.toJSON {
      agents = map (agent: {
        inherit (agent) command;
        inherit (agent.package) version;
      }) agents;
    };

    home.file = lib.mkIf (cfg.instructions != null) (
      lib.genAttrs instructionFiles (_: {
        source = cfg.instructions;
      })
    );
  };
}
