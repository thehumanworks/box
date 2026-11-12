"""Initial Pi authentication transfer, never part of an image or registry record."""

import json
from pathlib import Path

# stdin carries the credential, not argv/env or build inputs. link() publishes a
# complete private file without replacing credentials refreshed by Pi after an
# interrupted setup. A retry can safely observe that completed installation.
INSTALL_PI_AUTH = """
import json, os, stat, sys, tempfile
from pathlib import Path
path = Path(sys.argv[1])
data = sys.stdin.buffer.read()
if not isinstance(json.loads(data), dict):
    raise ValueError("Invalid auth object")
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
path.parent.chmod(0o700)
fd, temporary = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
try:
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
    try:
        os.link(temporary, path)
    except FileExistsError:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise ValueError("Existing auth must be a private regular file")
finally:
    Path(temporary).unlink(missing_ok=True)
"""


def read_pi_auth() -> bytes:
    path = Path.home() / ".pi/agent/auth.json"
    try:
        data = path.read_bytes()
        if len(data) > 1024 * 1024 or not isinstance(json.loads(data), dict):
            raise ValueError
    except OSError, ValueError:
        # JSON/parser errors can quote credential contents: never forward them.
        raise ValueError(
            "Cannot read a valid ~/.pi/agent/auth.json; authenticate Pi on this client "
            "before creating a box or finishing its initial agent setup"
        ) from None
    return data


def install_pi_auth(sb) -> None:
    data = read_pi_auth()
    try:
        process = sb.exec(
            "/usr/local/bin/python",
            "-c",
            INSTALL_PI_AUTH,
            "/root/.pi/agent/auth.json",
            timeout=30,
            workdir="/",
        )
        process.stdin.write(data)
        process.stdin.write_eof()
        process.stdin.drain()
        if process.wait() != 0:
            raise RuntimeError
    except Exception:
        raise ValueError("Pi authentication transfer was not confirmed; retry box up") from None
