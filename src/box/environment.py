"""The reproducible development image and its private Tailscale network."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import modal

ASSETS = Path(__file__).with_name("assets")
# Prevent BASH_ENV from restoring a snapshot token before boot refreshes it.
ENTRYPOINT = ["/usr/bin/env", "-u", "BASH_ENV", "/bin/bash", "/opt/box/boot.sh"]
PATH = (
    "/root/.local/share/mise/shims:/root/.local/bin:/root/.nix-profile/bin:"
    "/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)


def build_image() -> modal.Image:
    """Install tools through Nix; apt supplies only the installer prerequisites."""
    image = (
        modal.Image.debian_slim(python_version="3.14")
        .apt_install("ca-certificates", "curl", "xz-utils", "procps")
        .add_local_dir(ASSETS / "home", "/opt/box/home", copy=True)
        .add_local_file(ASSETS / "install.sh", "/opt/box/install.sh", copy=True)
        .add_local_file(ASSETS / "shell.sh", "/opt/box/shell.sh", copy=True)
        .run_commands("bash /opt/box/install.sh")
    )
    # Runtime changes do not reinstall the pinned Home Manager environment.
    for name in ("boot.sh", "boot.py", "ssh_env.py", "network.py", "rex_network.py"):
        image = image.add_local_file(ASSETS / name, "/opt/box/" + name, copy=True)
    return image.env(
        {
            "HOME": "/root",
            "USER": "root",
            "LOGNAME": "root",
            "REX_RUNTIME_DIR": "/run/box/rex",
            "PATH": PATH,
            "BASH_ENV": "/etc/profile.d/box.sh",
            "ENV": "/etc/profile.d/box.sh",
            "NIX_SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "NIX_REMOTE": "local",
            "DETSYS_IDS_TELEMETRY": "disabled",
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }
    ).workdir("/root")


def start_network(
    sb: modal.Sandbox,
    hostname: str,
    auth_key: str | None = None,
    secret_name: str | None = None,
) -> dict[str, str]:
    """Join SSH and Rex to the tailnet, reusing saved identities on resume.

    Credentials are injected only into this exec, never the image or boot process.
    The Modal secret accepts TAILSCALE_AUTHKEY, TAILSCALE_AUTH_KEY or TS_AUTHKEY.
    """

    def run(**credentials):
        process = sb.exec(
            "/usr/local/bin/python",
            "/opt/box/network.py",
            hostname,
            timeout=240,
            **credentials,
        )
        return process.stdout.read(), process.stderr.read(), process.wait()

    # Resuming a saved identity must work even if its original secret was deleted.
    stdout, stderr, code = run()
    if code == 78:
        if auth_key:
            stdout, stderr, code = run(env={"TAILSCALE_AUTHKEY": auth_key})
        elif secret_name:
            stdout, stderr, code = run(secrets=[modal.Secret.from_name(secret_name)])
    if code != 0:
        detail = stderr.strip() or "Tailscale startup failed"
        if auth_key:
            detail = detail.replace(auth_key, "[redacted]")
        raise RuntimeError(detail)
    try:
        result = json.loads(stdout)
        address = ipaddress.ip_address(result["tailscale_ip"])
        if address not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("address is outside the Tailscale IPv4 range")
        if result["host"] != str(address) or result["hostname"] != hostname:
            raise ValueError("network identity does not match this box")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Tailscale readiness response") from exc
    return result
