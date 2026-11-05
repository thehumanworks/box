"""Refresh root-only SSH credentials from the current sandbox's injected secrets.

Filesystem snapshots can contain this file. Every boot replaces it before SSH
starts, so resumed boxes use the newly injected token rather than a saved token.
"""

import os
import shlex
import tempfile
from pathlib import Path


def write_environment(directory: Path = Path("/run/box")) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
    content = (
        f"export OP_SERVICE_ACCOUNT_TOKEN={shlex.quote(token)}\n"
        if token
        else "unset OP_SERVICE_ACCOUNT_TOKEN\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=".ssh-env-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
        os.replace(temporary, directory / "ssh-env.sh")
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    write_environment()
