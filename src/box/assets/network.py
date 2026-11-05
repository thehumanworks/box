"""Runs inside the sandbox; stdout is one readiness JSON object."""

import ipaddress
import json
import os
import subprocess
import sys
import time

SOCKET = "/run/box/tailscaled.sock"
TAILSCALE = "/root/.nix-profile/bin/tailscale"


class LoginRequired(RuntimeError):
    """The daemon has no reusable login; retry with enrollment credentials."""


def command(*args, auth_key=None):
    result = subprocess.run(
        [TAILSCALE, f"--socket={SOCKET}", *args],
        input=auth_key + "\n" if auth_key else None,
        text=True,
        capture_output=True,
        timeout=65,
    )
    if result.returncode:
        # Never relay credential-bearing daemon/CLI diagnostics to the caller.
        raise RuntimeError(f"Tailscale {args[0]} failed; inspect the sandbox daemon log")
    return result.stdout


def status():
    try:
        # `status` returns nonzero before enrollment but still emits valid JSON.
        result = subprocess.run(
            [TAILSCALE, f"--socket={SOCKET}", "status", "--json"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return json.loads(result.stdout)
    except ValueError, subprocess.TimeoutExpired:
        return {}


def connect(hostname):
    keys = [
        os.environ.pop(name, "")
        for name in ("TAILSCALE_AUTHKEY", "TAILSCALE_AUTH_KEY", "TS_AUTHKEY")
    ]
    auth_key = next((key for key in keys if key), "")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = status()
        if state.get("BackendState") in {"Running", "NeedsLogin", "Stopped", "NeedsMachineAuth"}:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("Tailscale daemon did not become ready within 25 seconds")

    if state["BackendState"] == "NeedsMachineAuth":
        raise RuntimeError("This Tailscale device requires approval in the tailnet admin console")
    if state["BackendState"] == "Running":
        command("set", "--ssh", "--accept-dns=false", f"--hostname={hostname}")
    else:
        args = ["up", "--ssh", "--accept-dns=false", f"--hostname={hostname}", "--timeout=60s"]
        if state["BackendState"] == "NeedsLogin":
            if not auth_key:
                raise LoginRequired(
                    "Tailscale login required: provide an auth key or a Modal secret "
                    "containing TAILSCALE_AUTHKEY"
                )
            args.append("--auth-key=file:/dev/stdin")
        command(*args, auth_key=auth_key)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = status()
        if state.get("BackendState") == "Running" and state.get("Self", {}).get("Online"):
            for value in state.get("TailscaleIPs", []):
                address = ipaddress.ip_address(value)
                if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
                    return {
                        "host": str(address),
                        "tailscale_ip": str(address),
                        "hostname": hostname,
                    }
        time.sleep(0.5)
    raise RuntimeError("Tailscale did not report an online IPv4 address within 20 seconds")


if __name__ == "__main__":
    from rex_network import LoginRequired as RexLoginRequired
    from rex_network import connect as connect_rex

    try:
        # Each node enrolls separately; neither daemon inherits the reusable key.
        key = next(
            (
                os.environ[name]
                for name in ("TAILSCALE_AUTHKEY", "TAILSCALE_AUTH_KEY", "TS_AUTHKEY")
                if os.environ.get(name)
            ),
            None,
        )
        result = connect(sys.argv[1])
        result.update(connect_rex("rex-" + sys.argv[1], auth_key=key))
        print(json.dumps(result))
    except (LoginRequired, RexLoginRequired) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(78)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
