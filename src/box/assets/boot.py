"""Supervise the SSH and Rex daemons without systemd or user shell hooks."""

import os
import signal
import subprocess
import sys
from pathlib import Path

RUNTIME = Path("/run/box")
TAILSCALE_STATE = Path("/var/lib/box/tailscale")
REX_STATE = Path("/root/.local/state/rex")
PROFILE = "/root/.nix-profile/bin/"
PATH = (
    "/root/.local/share/mise/shims:/root/.local/bin:/root/.nix-profile/bin:"
    "/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)


def runtime_environment():
    env = dict(os.environ)
    for name in (
        "OP_SERVICE_ACCOUNT_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "TAILSCALE_AUTHKEY",
        "TAILSCALE_AUTH_KEY",
        "TS_AUTHKEY",
        "BASH_ENV",
        "ENV",
    ):
        env.pop(name, None)
    env.update(
        PATH=PATH,
        REX_RUNTIME_DIR=str(RUNTIME / "rex"),
    )
    return env


def supervise(commands, env, *, logs=None):
    """An exited service ends the box; always reap and stop its sibling."""
    children = []

    def stop(signum, frame):
        raise SystemExit(0)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for index, command in enumerate(commands):
            log = logs[index] if logs else None
            children.append(
                subprocess.Popen(
                    command, env=env, stdout=log, stderr=subprocess.STDOUT if log else None
                )
            )
        while True:
            pid, status = os.wait()
            for child in children:
                if child.pid == pid:
                    code = os.waitstatus_to_exitcode(status)
                    child.returncode = code
                    return code if code >= 0 else 128 - code
            # A container init may also inherit exited grandchildren.
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    from ssh_env import write_environment

    os.umask(0o077)
    for directory in (RUNTIME, RUNTIME / "rex", TAILSCALE_STATE, REX_STATE):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Refresh snapshot credentials before starting either daemon or any shell.
    write_environment()
    for socket in (RUNTIME / "tailscaled.sock", RUNTIME / "rex/server.sock"):
        socket.unlink(missing_ok=True)
    commands = [
        [
            PROFILE + "tailscaled",
            "--tun=userspace-networking",
            f"--state={TAILSCALE_STATE}/tailscaled.state",
            f"--socket={RUNTIME}/tailscaled.sock",
        ],
        [PROFILE + "rex", "server", "run", "--log-format", "text"],
    ]
    with (
        (TAILSCALE_STATE / "daemon.log").open("a") as network_log,
        (REX_STATE / "server.log").open("a") as rex_log,
    ):
        return supervise(commands, runtime_environment(), logs=[network_log, rex_log])


if __name__ == "__main__":
    sys.exit(main())
