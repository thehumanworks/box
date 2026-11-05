"""HTTPS-only CLI client. No Modal SDK calls, automatic retries or redirects."""

import json
import os
import re
import ssl
import stat
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from box.config import name_value, remote_path

SYNC_TIMEOUT = 90
MAX_TIMEOUT = 3600
CALL_ID = re.compile(r"fc-[A-Za-z0-9]{1,125}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # Two 1 MiB streams, including JSON escaping.


class RemoteClientError(Exception):
    def __init__(self, message, *, request_id=None, call_id=None, exit_code=1):
        super().__init__(message)
        self.request_id = request_id
        self.call_id = call_id
        self.exit_code = exit_code

    def result(self):
        value = {"error": {"type": type(self).__name__, "message": str(self)}}
        if self.request_id:
            value["request_id"] = self.request_id
        if self.call_id:
            value["call_id"] = self.call_id
        return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deployment_file(path=None):
    if path:
        return Path(path).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates = sorted((root / "box" / "box-exec-api").glob("*/*/deployment.json"))
    if len(candidates) == 1:
        return candidates[0]
    message = (
        "No remote deployment found" if not candidates else "Multiple remote deployments found"
    )
    raise RemoteClientError(
        message + ". Select --remote-deployment PATH or BOX_REMOTE_DEPLOYMENT. "
        "Provision with scripts/deploy_remote.py if needed."
    )


def read_json(path, *, private=False):
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if private:
            flags |= os.O_NOFOLLOW
        with os.fdopen(os.open(path, flags), "r") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise RemoteClientError("Remote configuration must be a small regular JSON file")
            if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise RemoteClientError("Proxy credentials must be owned by you with mode 0600")
            value = json.load(source)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except OSError, ValueError:
        # Invalid JSON diagnostics can contain secrets; never echo file contents.
        kind = "private proxy credentials" if private else "deployment manifest"
        raise RemoteClientError(
            f"Cannot read {kind}; check the file, permissions and JSON format"
        ) from None


class RemoteClient:
    def __init__(self, path=None):
        manifest = deployment_file(path)
        deployment = read_json(manifest)
        try:
            workspace, environment = deployment["workspace"], deployment["environment"]
            self.url = deployment["url"].rstrip("/")
            parsed = urlsplit(self.url)
            if (
                deployment["app"] != "box-exec-api"
                or not isinstance(workspace, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", workspace)
                or not isinstance(environment, str)
                or not environment
                or parsed.scheme != "https"
                or not parsed.hostname
                or not parsed.hostname.endswith(".modal.run")
                or not parsed.hostname.startswith(f"{workspace.lower()}--box-exec-api-")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError()
            credential_path = Path(deployment["credentials_file"]).expanduser()
            if not credential_path.is_absolute():
                credential_path = manifest.resolve().parent / credential_path
        except KeyError, TypeError, ValueError, AttributeError:
            raise RemoteClientError(
                "Invalid deployment manifest: expected this app's HTTPS Modal URL"
            ) from None
        credentials = read_json(credential_path, private=True)
        if (
            credentials.get("workspace") != workspace
            or credentials.get("environment") != environment
        ):
            raise RemoteClientError(
                "Proxy credentials do not match the deployment workspace/environment"
            )
        key, secret = credentials.get("token_id"), credentials.get("token_secret")
        if not all(
            isinstance(v, str) and re.fullmatch(pattern, v)
            for v, pattern in (
                (key, r"wk-[A-Za-z0-9_-]+"),
                (secret, r"ws-[A-Za-z0-9_-]+"),
            )
        ):
            raise RemoteClientError(
                "Expected Modal proxy credentials (wk-/ws-), not API credentials"
            )
        self._headers = {"Modal-Key": key, "Modal-Secret": secret}
        self._opener = build_opener(
            NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    def execute(self, name, command, *, cwd=None, timeout=60, request_id=None):
        return self._submit("exec", name, command, cwd, timeout, request_id)

    def spawn(self, name, command, *, cwd=None, timeout=MAX_TIMEOUT, request_id=None):
        return self._submit("spawn", name, command, cwd, timeout, request_id)

    def _submit(self, operation, name, command, cwd, timeout, request_id):
        name = name_value(name)
        cwd = remote_path(cwd, allow_root=True) if cwd is not None else None
        request_id = uuid.uuid4().hex if request_id is None else request_id
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError(
                "request_id must be 8–128 ASCII letters, digits, underscores or hyphens"
            )
        limit = SYNC_TIMEOUT if operation == "exec" else MAX_TIMEOUT
        if type(timeout) is not int or not 1 <= timeout <= limit:
            raise ValueError(f"{operation} timeout must be 1–{limit} seconds")
        if (
            not command
            or len(command) > 256
            or not command[0]
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or sum(len(arg.encode("utf-8")) for arg in command) > 65536
        ):
            raise ValueError(
                "Expected nonempty literal argv, no NUL, at most 256 arguments / 64 KiB"
            )
        return self._request(
            "/" + operation,
            body={
                "name": name,
                "command": list(command),
                "cwd": cwd,
                "timeout": timeout,
                "request_id": request_id,
            },
            timeout=125,
            request_id=request_id,
        )

    def poll(self, call_id, *, timeout=30):
        if not CALL_ID.fullmatch(call_id):
            raise ValueError("Expected a Modal call ID (fc- followed by letters and digits)")
        return self._request("/poll/" + call_id, timeout=timeout, call_id=call_id)

    def _request(self, path, *, body=None, timeout, request_id=None, call_id=None):
        recovery = (
            f" Execution may have started. Retry identical input with --request-id {request_id}; "
            "do not submit under a new ID."
            if request_id
            else f" Retry box poll {call_id}, not command submission."
        )
        request = Request(
            self.url + path,
            method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                **self._headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            try:
                stream = self._opener.open(request, timeout=timeout)
            except HTTPError as error:
                stream = error  # Read bounded structured failures without following redirects.
            with stream:
                status = stream.code
                raw = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError()
            if status in (401, 403):
                raise RemoteClientError(
                    f"Proxy authentication rejected (HTTP {status}). Check the deployment's "
                    "proxy credentials; Modal API tokens cannot authenticate these endpoints.",
                    request_id=request_id,
                    call_id=call_id,
                )
            try:
                value = json.loads(raw)
            except ValueError:
                raise RemoteClientError(
                    f"Remote service returned HTTP {status} without a JSON result." + recovery,
                    request_id=request_id,
                    call_id=call_id,
                ) from None
            if not valid_response(value, status, path):
                raise ValueError()
            # Keep error diagnostics safe, including failed results nested by /poll.
            outcome = value.get("result", value)
            if "error" in outcome:
                text = json.dumps(outcome["error"])
                for secret in self._headers.values():
                    text = text.replace(secret, "[redacted]")
                outcome["error"] = json.loads(text)
                if request_id:
                    outcome["request_id"] = outcome.get("request_id") or request_id
                if call_id:
                    outcome["call_id"] = outcome.get("call_id") or call_id
            return value
        except KeyboardInterrupt:
            raise RemoteClientError(
                "Interrupted." + recovery, request_id=request_id, call_id=call_id, exit_code=130
            ) from None
        except OSError, URLError, HTTPException:
            raise RemoteClientError(
                "HTTPS request failed; check network and TLS trust." + recovery,
                request_id=request_id,
                call_id=call_id,
            ) from None
        except ValueError, TypeError, RecursionError:
            raise RemoteClientError(
                "Invalid or oversized remote response." + recovery,
                request_id=request_id,
                call_id=call_id,
            ) from None


def valid_outcome(value):
    if not isinstance(value, dict):
        return False
    if value.get("status") == "completed":
        return (
            type(value.get("exit_code")) is int
            and 0 <= value["exit_code"] <= 255
            and all(isinstance(value.get(key), str) for key in ("stdout", "stderr"))
            and all(
                type(value.get(key)) is bool for key in ("stdout_truncated", "stderr_truncated")
            )
        )
    return value.get("status") == "failed" and isinstance(value.get("error"), dict)


def valid_response(value, status, path):
    if not isinstance(value, dict):
        return False
    if status >= 400:
        return (value.get("status") == "failed" and isinstance(value.get("error"), dict)) or (
            status == 410 and value.get("status") == "expired"
        )
    if path == "/exec":
        return status == 200 and valid_outcome(value)
    if path == "/spawn":
        return (
            status == 202
            and value.get("status") == "submitted"
            and isinstance(value.get("call_id"), str)
            and bool(CALL_ID.fullmatch(value["call_id"]))
        )
    if status == 202:
        return value.get("status") == "pending" and value.get("call_id") == path.removeprefix(
            "/poll/"
        )
    return (
        status == 200
        and valid_outcome(value.get("result"))
        and value.get("status") == value["result"]["status"]
    )
