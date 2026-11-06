"""Deploy the exec service using credentials injected into this process only.

Run from the repository: dotenvx run --quiet --strict -f .env -- \
    uv run python scripts/deploy_remote.py

The "remote" extra (pydantic, FastAPI) is installed in the Modal image, not locally.
"""

import argparse
import json
import os
from pathlib import Path

import modal

from box.modal_transport import connect


def proxy_credentials(workspace, environment, directory):
    """Create once, store privately, and never print or upload client credentials."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "proxy.json"
    if path.is_symlink():
        raise ValueError("Refusing a symlink for proxy credentials")
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ValueError(f"Proxy credentials must be private: chmod 600 {path}")
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            raise ValueError(
                "Credential provisioning was interrupted. Inspect this app's workspace proxy "
                "tokens before removing the incomplete proxy.json and provisioning again."
            ) from None
        if value["workspace"] != workspace.name or value["environment"] != environment:
            raise ValueError("Stored proxy credentials belong to a different workspace/environment")
    else:
        # Reserve the path before creating a token, avoiding duplicate token
        # creation by concurrent deploy processes. No secret enters argv/stdout.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        token = None
        try:
            with os.fdopen(fd, "w") as output:
                token = workspace.proxy_tokens.create(name=f"box-exec-api-{environment}")
                value = {
                    "workspace": workspace.name,
                    "environment": environment,
                    "token_id": token.token_id,
                    "token_secret": token.token_secret,
                }
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            # If the token is known but could not be stored, revoke only this
            # freshly-created token. An uncertain create leaves the reservation
            # for manual inspection rather than repeatedly minting more tokens.
            if token is not None:
                workspace.proxy_tokens.delete(token.token_id)
                path.unlink()
            raise
    token_info = next(
        (item for item in workspace.proxy_tokens.list() if item.token_id == value["token_id"]), None
    )
    if token_info is None:
        raise ValueError("Stored proxy token was revoked; provision a replacement explicitly")
    # Only RBAC-enabled workspaces issue environment-scoped tokens. Unscoped
    # tokens are workspace-wide and the allow API rejects them.
    if token_info.scoped:
        workspace.proxy_tokens.allow(value["token_id"], environment)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", help="Modal environment; default is the active environment."
    )
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parents[1])
    connect()
    workspace = modal.Workspace.from_context()
    workspace.hydrate()
    environment = (
        args.environment
        or modal.config.config.get("environment")
        or workspace.settings.list().default_environment
    )
    from box.remote_app import app, web

    with modal.enable_output():
        app.deploy(environment_name=environment)
    directory = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "box"
        / app.name
        / workspace.name
        / environment
    )
    credentials_file = proxy_credentials(workspace, environment, directory)
    deployment = {
        "app": app.name,
        "app_id": app.app_id,
        "workspace": workspace.name,
        "environment": environment,
        "url": web.get_web_url(),
        "credentials_file": str(credentials_file),
    }
    manifest = directory / "deployment.json"
    manifest.write_text(json.dumps(deployment, indent=2) + "\n")
    print(json.dumps({**deployment, "deployment_file": str(manifest)}))


if __name__ == "__main__":
    main()
