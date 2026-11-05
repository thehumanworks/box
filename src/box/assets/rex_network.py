"""Enroll Rex's own Tailscale node using a transient key over its local socket."""

import json
import os
import subprocess
import time
from urllib.parse import urlsplit

REX = "/root/.nix-profile/bin/rex"
SOCKET = "unix:///run/box/rex/server.sock"
CLI = [REX, "--server", SOCKET, "--autostart=false", "--timeout", "10s"]


class LoginRequired(RuntimeError):
    """Retry enrollment with the existing Tailscale secret."""


def command(*args, auth_key=None):
    env = dict(os.environ)
    for name in (
        "OP_SERVICE_ACCOUNT_TOKEN",
        "TAILSCALE_AUTHKEY",
        "TAILSCALE_AUTH_KEY",
        "TS_AUTHKEY",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [*CLI, *args],
        input=auth_key + "\n" if auth_key else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Rex setup failed; inspect /root/.local/state/rex/server.log")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("Invalid Rex readiness response") from exc


def status():
    try:
        return command("server", "status", "--json")
    except RuntimeError, subprocess.TimeoutExpired:
        return {}


def connect(hostname, auth_key=None):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        server = status()
        if server.get("host_id") and isinstance(server.get("tailscale"), dict):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("Rex server did not become ready within 25 seconds")

    state = server["tailscale"]
    if state.get("state") == "needs_login":
        # Rex ignores a new key while its node is already enabled.
        command("server", "tailscale", "off", "--json")
        state = {"state": "disabled"}
    args = ["server", "tailscale", "on", "--hostname", hostname, "--json"]
    enrolling = state.get("state") == "disabled" and bool(auth_key)
    if enrolling:
        args.extend(["--auth-key-file", "-"])
    # Omitting --allow-tailnet-members explicitly retains Rex's owner-only policy.
    command(*args, auth_key=auth_key if enrolling else None)

    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        server = status()
        state = server.get("tailscale", {})
        if state.get("state") == "needs_login":
            if auth_key:
                # Authentication is asynchronous; NeedsLogin can be transient
                # while tsnet exchanges the supplied enrollment key.
                time.sleep(0.5)
                continue
            command("server", "tailscale", "off", "--json")
            raise LoginRequired("Rex Tailscale login required: provide the Tailscale auth key")
        if state.get("state") == "needs_machine_approval":
            raise RuntimeError("This Rex device requires approval in the tailnet admin console")
        if state.get("tags"):
            raise RuntimeError("Rex needs an untagged, user-owned Tailscale auth key")
        if state.get("state") == "disabled" and state.get("error"):
            raise RuntimeError(
                "Rex HTTPS listener failed; check tailnet HTTPS certificate settings"
            )
        if state.get("state") == "running" and state.get("https", {}).get("state") == "available":
            dns = state.get("dns_name", "").rstrip(".")
            url = state.get("https", {}).get("url", "")
            parsed = urlsplit(url)
            if (
                not dns.startswith(hostname + ".")
                or parsed.scheme != "https"
                or parsed.netloc != dns
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or state.get("allow_tailnet_members") is not False
                or not server.get("host_id")
            ):
                raise RuntimeError("Invalid Rex endpoint or access policy")
            # On resume, the cached HTTPS certificate can be ready before tsnet
            # supplies its owner profile. Keep access fail-closed while it loads.
            if not (state.get("account") or {}).get("login_name"):
                time.sleep(0.5)
                continue
            return {
                "rex_url": "https://" + dns,
                "rex_hostname": dns,
                "rex_host_id": server["host_id"],
            }
        time.sleep(0.5)
    raise RuntimeError("Rex HTTPS did not become ready with an owner identity within 65 seconds")
