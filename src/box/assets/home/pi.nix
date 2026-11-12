# Mirrored from this host's Home Manager setup; no host SOPS paths or credentials.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.pi-coding-agent;
in
{
  home.file.".pi/agent/presets" = {
    source = ./pi/presets;
    recursive = true;
  };
  home.file.".pi/agent/agents/loggly-analyst.md".source = ./pi/agents/loggly-analyst.md;
  # Mirror the host-local extension too; it is inert outside a Herdr session.
  home.file.".pi/agent/extensions/herdr-agent-state.ts".source =
    ./pi/extensions/herdr-agent-state.ts;
  programs.pi-coding-agent = {
    enable = true;
    # Keep runtime settings writable; declared values/lists win on activation.
    # See the merger override below for authoritative package removal.
    mutableSettings = true;
    keybindings = {
      tui.editor.deleteWordBackward = [
        "ctrl+w"
        "alt+backspace"
      ];
      tui.editor.cursorUp = [
        "up"
        "ctrl+p"
      ];
      tui.editor.cursorDown = [
        "down"
        "ctrl+n"
      ];
    };
    settings = {
      defaultProvider = "openai";
      defaultModel = "gpt-6-astra";
      defaultThinkingLevel = "medium";
      hideThinkingBlock = true;
      advisorModel = "claude-bridge/claude-fable-5-1";
      enabledModels = [
        "openai/gpt-6.1-sol"
        "openai/gpt-6.1-terra"
        "openai/gpt-6-astra"
        "claude-bridge/claude-fable-5-1"
        "claude-bridge/claude-opus-5-5"
        "claude-bridge/claude-sonnet-5-5"
        "claude-bridge/claude-haiku-5-5"
      ];
      scopedModels = [
        "openai/gpt-6-astra"
        "openai/gpt-6.1-sol"
        "openai/gpt-6.1-terra"
        "anthropic/claude-fable-5-1"
        "anthropic/claude-opus-5-5"
        "anthropic/claude-sonnet-5-5"
        "anthropic/claude-haiku-5-5"
      ];
      packages = [
        "npm:pi-claude-bridge"
        "git:https://github.com/thehumanworks/delta-pi"
        "npm:@nothumanwork/pi-tps"
        "npm:@nothumanwork/pi-fast"
        "npm:@nothumanwork/pi-loggly"
        "npm:@nothumanwork/pi-idle-compaction"
        "npm:@nothumanwork/pi-background-compaction"
        "npm:@nothumanwork/pi-remote"
        "npm:@nothumanwork/pi-atlassian"
        "npm:@nothumanwork/pi-voice"
        "npm:@nothumanwork/pi-usage"
        "npm:@nothumanwork/pi-recall"
        "npm:@nothumanwork/pi-xai"
        "npm:@nothumanwork/pi-advisor"
        "npm:@nothumanwork/pi-create-document"
      ];
      backgroundCompaction = {
        enabled = true;
        model = "openai/gpt-6.1-terra";
      };
      idleCompaction = {
        enabled = true;
        tokenThreshold = 150000;
        idleMinutes = 5;
      };
      memory = {
        dream.model = "openai/gpt-6.1-terra";
        capture.model = "openai/gpt-6.1-terra";
      };
      compaction = {
        enabled = true;
      };
      retry = {
        enabled = true;
        maxRetries = 3;
      };
      defaultProjectTrust = "always";
      defaultTools = [
        "+codemode"
      ];
    };
  };
  # Upstream mutableSettings unions packages, retaining removed declarations.
  # Replace declared arrays (including packages) instead, while recursively
  # preserving undeclared runtime settings such as deviceId and UI preferences.
  # Removing an entire settings key still leaves its runtime value intact;
  # use packages = [] to explicitly clear all packages. Stop Pi before activation.
  home.activation.piCodingAgentMutableSettings = lib.mkForce (
    lib.hm.dag.entryAfter [ "linkGeneration" ] (
      lib.hm.generators.mkImpureConfigMerger {
        inherit pkgs;
        format = "json";
        empty = "{}";
        mode = "600";
        path = "${cfg.configDir}/settings.json";
        staticSettings = (pkgs.formats.json { }).generate "pi-settings.json" cfg.settings;
        jqOperation = "$dynamic * $static";
      }
    )
  );

  # The Agent SDK's bundled Linux binary expects an FHS loader. Use the
  # Nix-packaged Claude instead, while preserving the bridge's writable state.
  home.activation.claudeBridgeMutableSettings = lib.hm.dag.entryAfter [ "linkGeneration" ] (
    lib.hm.generators.mkImpureConfigMerger {
      inherit pkgs;
      format = "json";
      empty = "{}";
      mode = "600";
      path = "${cfg.configDir}/claude-bridge.json";
      staticSettings = (pkgs.formats.json { }).generate "claude-bridge-settings.json" {
        provider.pathToClaudeCodeExecutable = lib.getExe pkgs.claude-code;
      };
      jqOperation = "$dynamic * $static";
    }
  );

  home.file.".pi/agent/AGENTS.md".text =
    ''
      # Rules

      ## Before acting
      - Establish the goal: if the ask has no agreed goal, state one in a sentence and confirm it. Every later decision is judged against it.
      - Read before you write: inspect existing code, conventions and prior notes before changing anything. Match what's there unless the goal requires otherwise.
      - Assume cheaply, ask expensively: if a wrong guess is cheap to undo, state the assumption and proceed. If it's costly, irreversible or a design decision, ask first.

      ## While acting
      - Serve the goal, not the request: improve on the ask when it advances the goal, and say what you added. Outside the goal, flag it; don't act on it.
      - Prefer existing and simple: maintained libraries, repo helpers and standard tools over custom code; no abstractions for one caller, no config for values that don't vary. Hand-roll only when you can say why in one line.
      - Never fake green: don't weaken, skip or silence a test, lint, type check or error to get past it. Fix it or report it.
      - Stop when the ground shifts: if the task turns out bigger, different, or in conflict with the goal, stop and say so. Don't push through.

      ## Before reporting
      - Verify, don't infer: run it, read it, test it. "Should work" is not done.
      - Report state, not effort: what works, what doesn't, what wasn't tried. Lead with the result; the last sentence must stand alone.

      ## Across sessions
      - Write for the next agent: readable, searchable, small, unsurprising.
      - Learn, don't hoard: enforce a learning mechanically (test, lint, hook, config) when you can; otherwise one line here. Record failures with their fix. Keep only what changes a future action.
      - Tidy your own mess: remove scratch files, debug output and dead code you added. Never delete user work without asking.'';

  home.file.".pi/agent/mcp.json".text = ''
    {
      "mcpServers": {
        "exa": {
          "url": "https://mcp.exa.ai/mcp",
          "headers": {
            "x-api-key": "''${EXA_API_KEY}"
          }
        }
      }
    }'';
}
