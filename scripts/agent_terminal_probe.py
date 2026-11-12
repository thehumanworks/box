"""Run inside a test sandbox: real tmux PTYs, not print-mode substitutes."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def probe(mode):
    # Let tmux implement terminal queries/rendering instead of approximating a
    # terminal emulator. Its private server/socket and children die in finally.
    with tempfile.TemporaryDirectory(prefix="box-agent-pty-") as temporary:
        env = dict(os.environ, TERM="xterm-256color")
        if mode in {"claude-empty", "claude-minimal"}:
            env["CLAUDE_CONFIG_DIR"] = temporary
            if mode == "claude-minimal":
                Path(temporary, ".claude.json").write_text('{"hasCompletedOnboarding":true}')
        prompt = (
            "Reply only with the uppercase words BOX AUTH OK joined with underscores. No tools."
        )
        if mode == "pi":
            argv = ["pi", "--no-session", prompt]
        elif mode == "claude-configured":
            argv = ["claude", prompt]  # Exercise the host's default model too.
        else:
            argv = ["claude", "--model", "haiku", prompt]
        socket = str(Path(temporary, "tmux.sock"))

        def tmux(*args, check=True):
            return subprocess.run(
                ["tmux", "-S", socket, *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=check,
            )

        output = ""
        trusted = False
        try:
            tmux("new-session", "-d", "-x", "140", "-y", "40", "-c", "/workspace", *argv)
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                captured = tmux("capture-pane", "-p", "-S", "-500", check=False)
                if captured.returncode:
                    break
                output = captured.stdout
                lower = output.lower()
                if mode == "claude-empty" and "choose the text style" in lower:
                    return {"mode": mode, "onboarding": True}
                if mode != "claude-empty" and "BOX_AUTH_OK" in output:
                    return {"mode": mode, "authenticated_reply": True}
                if "yes, i trust this folder" in lower and not trusted:
                    # The empty test-owned workspace is ours. Do not seed trust
                    # in .claude.json or automatically accept any login prompt.
                    time.sleep(1)
                    if "❯ no, exit" in lower or "❯no, exit" in lower:
                        tmux("send-keys", "Down")
                        time.sleep(0.2)
                    tmux("send-keys", "Enter")
                    trusted = True
                time.sleep(0.3)
            for key in ("CLAUDE_CODE_OAUTH_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN"):
                if env.get(key):
                    output = output.replace(env[key], "[redacted]")
            raise RuntimeError(f"{mode} did not reach its expected screen/reply: {output[-2500:]}")
        finally:
            tmux("kill-server", check=False)


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1])))
