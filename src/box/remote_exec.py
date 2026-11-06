"""Validated, replay-guarded execution shared by the SDK and HTTP entrypoints."""

import hashlib
import json

import modal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from box.config import name_value, remote_path
from box.remote_constants import MAX_TIMEOUT

MAX_OUTPUT_BYTES = 1024 * 1024  # Per stream; keep cached results under 5 MiB.
SYNC_TIMEOUT = 90


class ExecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    command: list[str] = Field(min_length=1, max_length=256)
    cwd: str | None = None
    timeout: int = Field(default=60, ge=1, le=MAX_TIMEOUT)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        return name_value(value)

    @field_validator("cwd")
    @classmethod
    def valid_cwd(cls, value):
        return remote_path(value, allow_root=True) if value is not None else None

    @model_validator(mode="after")
    def valid_command(self):
        if not self.command[0] or any("\x00" in arg for arg in self.command):
            raise ValueError("Command must have a nonempty executable and no NUL characters")
        if sum(len(arg.encode("utf-8")) for arg in self.command) > 64 * 1024:
            raise ValueError("Command arguments must not exceed 64 KiB")
        return self


def failure(request_id, call_id, code, message, http_status):
    return {
        "request_id": request_id,
        "call_id": call_id,
        "status": "failed",
        "error": {"code": code, "message": message, "http_status": http_status},
    }


def runtime_service():
    from box.service import Service
    from box.state import REGISTRY_NAME, Registry

    # The Modal runtime already supplies scoped credentials and a connected SDK.
    # Do not apply local enterprise-network patches or create a missing registry.
    return Service(Registry(data=modal.Dict.from_name(REGISTRY_NAME)))


def execute_claimed(request: ExecRequest, call_id: str, runs, service_factory=runtime_service):
    """Claim before dispatch; a lost worker/result never licenses another command."""
    request_id = request.request_id or call_id
    payload = request.model_dump(exclude={"request_id"})
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    key = "request:" + request_id
    # Both SDK and HTTP-created executions can subsequently be polled, but
    # arbitrary other FunctionCall IDs in this workspace cannot be read here.
    runs.put("call:" + call_id, {"request_id": request_id})
    claim = {"fingerprint": fingerprint, "call_id": call_id, "outcome": None}
    if not runs.put(key, claim, skip_if_exists=True):
        previous = runs.get(key)
        if previous is None or previous["fingerprint"] != fingerprint:
            return failure(
                request_id,
                call_id,
                "request_conflict",
                "Request ID is already claimed for different input, or its claim is unavailable.",
                409,
            )
        if previous["outcome"] is not None:
            return previous["outcome"]
        return failure(
            request_id,
            previous["call_id"],
            "execution_unconfirmed",
            "This request is running or its outcome is unknown. Poll the original call; "
            "do not resubmit under a new request ID without inspecting the sandbox.",
            409,
        )

    try:
        value = service_factory().execute(
            request.name,
            request.command,
            cwd=request.cwd,
            timeout=request.timeout,
            capture=True,
            max_output_bytes=MAX_OUTPUT_BYTES,
        )
        if value["exit_code"] == -1:
            # Modal 1.6.3 wait() converts ExecTimeoutError into -1. That is a
            # deadline sentinel, not a successful/ordinary command exit.
            outcome = {
                **failure(
                    request_id,
                    call_id,
                    "exec_timeout",
                    "Sandbox command timed out. Inspect the sandbox before another execution.",
                    504,
                ),
                **value,
            }
        else:
            outcome = {
                "request_id": request_id,
                "call_id": call_id,
                "status": "completed",
                **value,
            }
    except modal.exception.ExecTimeoutError:
        outcome = failure(
            request_id,
            call_id,
            "exec_timeout",
            "Sandbox command timed out. Inspect the sandbox before starting another execution.",
            504,
        )
    except modal.exception.NotFoundError:
        outcome = failure(request_id, call_id, "not_found", "Sandbox or registry not found.", 404)
    except ValueError as exc:
        # Service's validation/readiness errors contain no credentials or command output.
        outcome = failure(request_id, call_id, "box_unavailable", str(exc), 409)
    except Exception:
        # SDK errors may contain credentials or argv. Never send their raw text or
        # automatically rerun a command whose dispatch may have succeeded.
        outcome = failure(
            request_id,
            call_id,
            "execution_error",
            "Execution could not be confirmed. Inspect the sandbox before starting another call.",
            503,
        )
    claim["outcome"] = outcome
    try:
        runs.put(key, claim)
    except Exception:
        return failure(
            request_id,
            call_id,
            "result_not_recorded",
            "Execution finished but its result could not be saved. Do not rerun blindly.",
            503,
        )
    return outcome
