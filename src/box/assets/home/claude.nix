{ lib, pkgs, ... }:
let
  onboarding = pkgs.writeText "claude-onboarding.json" ''{"hasCompletedOnboarding":true}'';
in
{
  # Required by the host's TypeScript language-server plugin.
  home.packages = [
    pkgs.typescript-language-server
    pkgs.typescript
  ];
  # Onboarding is UI state, not authentication. Never copy the host account/cache.
  # Seed only when absent; Claude must remain free to update this file.
  home.activation.claudeOnboarding = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    if [ ! -e "$HOME/.claude.json" ]; then
      run ${pkgs.coreutils}/bin/install -m 600 ${onboarding} "$HOME/.claude.json"
    fi
  '';
  programs.claude-code = {
    enable = true;
    mutableSettings = true;
    settings = {
      model = "opus";
      effortLevel = "medium";
      advisorModel = "fable";
      tui = "fullscreen";
      remoteControlAtSetup = true;
      askUserQuestionTimeout = "5m";
      attribution = {
        commit = "";
        pr = "";
        sessionUrl = false;
      };
      enabledPlugins = {
        "typescript-lsp@claude-plugins-official" = true;
        "delta@zed-industries" = true;
      };
      extraKnownMarketplaces.zed-industries.source = {
        source = "github";
        repo = "zed-industries/delta-claude";
      };
      modelSettings.claude-opus-5-5 = {
        effortLevel = "xhigh";
        autoCompactWindow = "auto";
      };
      skipDangerousModePermissionPrompt = true;
      preferredNotifChannel = "auto";
      remoteControlAtStartup = true;
      inputNeededNotifEnabled = true;
      agentPushNotifEnabled = true;
      autoCompactEnabled = true;
      autoCompactWindow = 500000;
      autoMemoryEnabled = true;
      autoScrollEnabled = true;
      awaySummaryEnabled = true;
      copyFullResponse = true;
      copyOnSelect = true;
      disableClaudeAiConnectors = true;
      enableAllProjectMcpServers = true;
      enableArtifact = true;
      enableWorkflows = true;
      env = {
        IS_SANDBOX = "1";
        CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS = "150000";
      };
      feedbackDrafts = "off";
      idleCompaction = true;
      permissions = {
        defaultMode = "auto";
      };
      promptCacheTtl = "1h";
      subagentPromptCacheTtl = "1h";
      showThinkingSummaries = true;
      respondToBashCommands = false;
      spellcheck = {
        enabled = true;
        language = "en_GB";
      };
      teammateMode = "tmux";
      theme = "auto";
      voice = {
        enabled = true;
        mode = "tap";
      };
    };
  };
}
