"""Opt-in real image/agent/auth/SSH/resume check; only test-owned resources."""

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from smoke import cleanup_test_box, verify_rex_environment

from box.agents import read_pi_auth
from box.config import Config
from box.environment import ASSETS
from box.service import Service


def main():
    service = Service()
    name = "box-agents-smoke-" + uuid.uuid4().hex[:8]
    evidence = {"name": name, "success": False, "checks": []}

    def command(argv, timeout=60):
        # Modal leaves exec stdin open. Pi print mode waits for EOF on pipes,
        # even with a prompt argument; these scripted commands have no input.
        outcome = service.execute(
            name,
            ["bash", "-c", 'exec "$@" </dev/null', "agent-smoke", *argv],
            capture=True,
            timeout=timeout,
        )
        if outcome["exit_code"] != 0:
            raise RuntimeError(
                f"Agent check exited {outcome['exit_code']}: {outcome['stderr'][-3000:]}"
            )
        return outcome["stdout"]

    def check(label):
        evidence["checks"].append(label)
        print(json.dumps({"verified": label}), flush=True)

    try:
        record = service.up(Config(name=name, timeout=1800))
        evidence["sandbox_id"] = record["sandbox_id"]
        print(json.dumps({"created": name, "sandbox_id": record["sandbox_id"]}), flush=True)
        expected = hashlib.sha256(read_pi_auth()).hexdigest()
        command(
            [
                "python",
                "-c",
                """import hashlib, os, stat, sys
from pathlib import Path
p = Path('/root/.pi/agent/auth.json')
assert stat.S_IMODE(p.stat().st_mode) == 0o600
assert not p.is_symlink()
assert hashlib.sha256(p.read_bytes()).hexdigest() == sys.argv[1]
assert os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
assert not Path('/root/.claude/.credentials.json').exists()
""",
                expected,
            ]
        )
        check("private Pi auth copy matches client; OAuth env present; no Claude credential file")
        command(
            [
                "python",
                "-c",
                """import json
from pathlib import Path
assert json.loads(Path("/root/.claude.json").read_text()) == {"hasCompletedOnboarding": True}
assert "deviceId" not in json.loads(Path("/root/.pi/agent/settings.json").read_text())
""",
            ]
        )
        check("fresh image has only the minimal Claude state and no cloned Pi device ID")
        verify_rex_environment(service, name, evidence)
        evidence["versions"] = command(["bash", "-c", "pi --version; claude --version"]).strip()
        command(
            [
                "python",
                "-c",
                """import json, os
from pathlib import Path
p = json.loads(Path('/root/.pi/agent/settings.json').read_text())
c = json.loads(Path('/root/.claude/settings.json').read_text())
assert len(p['packages']) == 15
assert p['advisorModel'] == 'claude-bridge/claude-fable-5-1'
assert c['enabledPlugins']['delta@zed-industries']
assert c['enabledPlugins']['typescript-lsp@claude-plugins-official']
assert json.loads(Path('/root/.claude.json').read_text())['hasCompletedOnboarding']
assert not Path('/root/.pi/agent/settings.json').is_symlink()
assert not Path('/root/.claude/settings.json').is_symlink()
b = json.loads(Path('/root/.pi/agent/claude-bridge.json').read_text())
assert os.access(b['provider']['pathToClaudeCodeExecutable'], os.X_OK)
""",
            ]
        )
        check("host settings, extensions, plugins and Nix Claude bridge configured")
        command(
            [
                "python",
                "-c",
                """import hashlib, json, sys
from pathlib import Path
root = Path("/root/.pi/agent")
extension = root / "extensions/herdr-agent-state.ts"
assert hashlib.sha256(extension.read_bytes()).hexdigest() == sys.argv[1]
settings = json.loads((root / "settings.json").read_text())
for source in settings["packages"]:
    if source.startswith("npm:"):
        assert (root / "npm/node_modules" / source[4:] / "package.json").is_file(), source
    else:
        assert (root / "git/github.com/thehumanworks/delta-pi/package.json").is_file()
plugins = json.loads(Path("/root/.claude/plugins/installed_plugins.json").read_text())["plugins"]
for name in ("typescript-lsp@claude-plugins-official", "delta@zed-industries"):
    assert plugins[name]
""",
                hashlib.sha256(
                    (ASSETS / "home/pi/extensions/herdr-agent-state.ts").read_bytes()
                ).hexdigest(),
            ]
        )
        check("all 15 Pi packages, the bundled Herdr extension and both Claude plugins installed")
        command(
            [
                "python",
                "-c",
                """import json
from pathlib import Path
p = Path("/root/.pi/agent/settings.json")
s = json.loads(p.read_text()); s["quietStartup"] = True
s["packages"].append("npm:box-smoke-must-be-removed")
p.write_text(json.dumps(s))
p = Path("/root/.claude.json")
s = json.loads(p.read_text()); s["numStartups"] = 37; p.write_text(json.dumps(s))
""",
            ]
        )
        command(["home-manager", "switch"], timeout=180)
        command(
            [
                "python",
                "-c",
                """import json
from pathlib import Path
s = json.loads(Path("/root/.pi/agent/settings.json").read_text())
assert s["quietStartup"] is True and len(s["packages"]) == 15
assert json.loads(Path("/root/.claude.json").read_text())["numStartups"] == 37
assert Path("/root/.pi/agent/auth.json").is_file()
""",
            ]
        )
        check("Home Manager reactivation replaces declared arrays and preserves runtime state")
        terminal = Path(__file__).with_name("agent_terminal_probe.py").read_text()
        for mode in ("claude-empty", "claude-minimal", "claude-configured", "pi"):
            observed = json.loads(command(["python", "-c", terminal, mode], timeout=180))
            evidence[mode] = observed
            check(mode + " real PTY")
        bridge = command(
            [
                "pi",
                "--provider",
                "claude-bridge",
                "--model",
                "claude-haiku-5-5",
                "--no-session",
                "-p",
                "Reply only BOX_BRIDGE_OK. Do not use tools.",
            ],
            timeout=180,
        )
        assert "BOX_BRIDGE_OK" in bridge, "Pi Claude bridge did not return an authenticated reply"
        check("Pi Claude bridge authenticates through the Nix Claude binary")
        ssh = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=15",
                f"root@{record['host']}",
                'test -n "$CLAUDE_CODE_OAUTH_TOKEN" && claude -p --model haiku '
                "'Reply only BOX_SSH_OK; do not use tools'",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert ssh.returncode == 0 and "BOX_SSH_OK" in ssh.stdout, "SSH agent check failed"
        check("real SSH shell receives OAuth token and Claude inference succeeds")
        service.pause(name)
        restored = service.up(Config(name=name), resume_only=True)
        assert restored["sandbox_id"] != record["sandbox_id"]
        resumed_reply = command(
            [
                "bash",
                "-c",
                "set -e; test -s /root/.pi/agent/auth.json; "
                'test -n "$CLAUDE_CODE_OAUTH_TOKEN"; '
                'claude -p --model haiku "Reply only BOX_RESUME_OK; do not use tools"',
            ]
        )
        assert "BOX_RESUME_OK" in resumed_reply, "Resumed Claude inference failed"
        check("snapshot resume retains Pi auth and refreshes OAuth shell credentials")
        evidence["success"] = True
    finally:
        try:
            cleanup_test_box(service, name)
            evidence["cleanup"] = "test box and network logins removed"
        except BaseException:
            evidence["success"] = False
            evidence["cleanup"] = "failed; inspect test-owned resources"
            raise
        finally:
            Path("docs/agents-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
