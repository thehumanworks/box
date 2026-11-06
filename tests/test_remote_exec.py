"""Offline safety contracts; scripts/smoke_remote.py proves the deployed path."""

import copy
import io
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import modal
import pytest
from pydantic import ValidationError

from box.remote_exec import MAX_OUTPUT_BYTES, ExecRequest, execute_claimed
from box.service import Service


class Runs:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def put(self, key, value, *, skip_if_exists=False):
        with self.lock:
            if skip_if_exists and key in self.data:
                return False
            self.data[key] = copy.deepcopy(value)
            return True

    def get(self, key):
        return copy.deepcopy(self.data.get(key))


def request(**changes):
    return ExecRequest.model_validate(
        {
            "name": "dev",
            "command": ["printf", "a b", "$(false)", ""],
            "request_id": "request-123",
            **changes,
        }
    )


def completed():
    return {
        "exit_code": 23,
        "stdout": "out\n",
        "stderr": "err\n",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_exec_claim_preserves_argv_cwd_and_exit_and_reuses_terminal_result():
    runs = Runs()
    execute = Mock(return_value=completed())
    factory = Mock(return_value=SimpleNamespace(execute=execute))
    req = request(cwd="/", timeout=12)
    first = execute_claimed(req, "fc-first", runs, factory)
    second = execute_claimed(req, "fc-second", runs, factory)
    assert first == second
    assert first["status"] == "completed" and first["exit_code"] == 23
    execute.assert_called_once_with(
        "dev",
        req.command,
        capture=True,
        cwd="/",
        timeout=12,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )
    assert runs.get("call:fc-first") == {"request_id": req.request_id}
    assert runs.get("call:fc-second") == {"request_id": req.request_id}
    assert "command" not in runs.get("request:" + req.request_id)


def test_modal_timeout_sentinel_is_not_an_ordinary_completed_exit():
    result = {**completed(), "exit_code": -1}
    factory = Mock(return_value=SimpleNamespace(execute=Mock(return_value=result)))
    outcome = execute_claimed(request(), "fc-test", Runs(), factory)
    assert outcome["status"] == "failed"
    assert outcome["error"]["code"] == "exec_timeout"
    assert outcome["error"]["http_status"] == 504
    assert outcome["exit_code"] == -1 and outcome["stdout"] == "out\n"


def test_conflicting_request_id_never_dispatches_again():
    runs = Runs()
    execute = Mock(return_value=completed())
    factory = Mock(return_value=SimpleNamespace(execute=execute))
    execute_claimed(request(), "fc-first", runs, factory)
    response = execute_claimed(request(timeout=20), "fc-second", runs, factory)
    assert response["error"]["code"] == "request_conflict"
    assert execute.call_count == 1


def test_concurrent_or_replayed_running_claim_never_launches_a_second_command():
    runs = Runs()
    started, finish = threading.Event(), threading.Event()
    calls = []

    def execute(*args, **kwargs):
        calls.append(True)
        started.set()
        assert finish.wait(5)
        return completed()

    factory = Mock(return_value=SimpleNamespace(execute=execute))
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(execute_claimed, request(), "fc-original", runs, factory)
        assert started.wait(5)
        try:
            duplicate = execute_claimed(request(), "fc-duplicate", runs, factory)
            assert duplicate["error"]["code"] == "execution_unconfirmed"
            assert duplicate["call_id"] == "fc-original"
        finally:
            finish.set()
        assert original.result()["status"] == "completed"
    assert calls == [True]


@pytest.mark.parametrize(
    "error,code",
    [
        (ValueError("dev setup is incomplete"), "box_unavailable"),
        (modal.exception.ExecTimeoutError(), "exec_timeout"),
        (modal.exception.NotFoundError("private diagnostic"), "not_found"),
        (RuntimeError("secret credential and command payload"), "execution_error"),
    ],
)
def test_operational_errors_are_terminal_without_replay_or_sensitive_diagnostics(error, code):
    runs = Runs()
    execute = Mock(side_effect=error)
    factory = Mock(return_value=SimpleNamespace(execute=execute))
    first = execute_claimed(request(), "fc-first", runs, factory)
    assert first["error"]["code"] == code
    assert "secret credential" not in str(first) and "private diagnostic" not in str(first)
    assert execute_claimed(request(), "fc-retry", runs, factory) == first
    assert execute.call_count == 1


def test_lost_result_keeps_claim_and_never_redispatches():
    runs = Runs()
    put = runs.put

    def fail_result(key, value, **kwargs):
        if value.get("outcome"):
            raise OSError("unavailable")
        return put(key, value, **kwargs)

    runs.put = fail_result
    execute = Mock(return_value=completed())
    factory = Mock(return_value=SimpleNamespace(execute=execute))
    first = execute_claimed(request(), "fc-first", runs, factory)
    assert first["error"]["code"] == "result_not_recorded"
    again = execute_claimed(request(), "fc-second", runs, factory)
    assert again["error"]["code"] == "execution_unconfirmed"
    assert execute.call_count == 1


def test_modal_declaration_constructs_without_deploying():
    from box.remote_app import app, exec_command, web

    assert app.name == "box-exec-api"
    assert callable(exec_command.remote) and callable(web.get_web_url)


def test_deploy_imports_the_app_without_the_remote_extra_installed_locally():
    # scripts/deploy_remote.py imports remote_app locally; pydantic and FastAPI are
    # installed only in the Modal image. A subprocess keeps this test's modules intact.
    code = (
        "import sys\n"
        "sys.modules['pydantic'] = sys.modules['fastapi'] = None\n"
        "from box.remote_app import app, exec_command, web\n"
        "assert app.name == 'box-exec-api'\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_claim_store_failure_is_a_terminal_worker_outcome(monkeypatch):
    from box import remote_app

    monkeypatch.setattr(remote_app.modal, "current_function_call_id", lambda: "fc-test")
    monkeypatch.setattr(
        remote_app, "runs", SimpleNamespace(put=Mock(side_effect=OSError("private")))
    )
    result = remote_app.exec_command.local(request().model_dump())
    assert result["status"] == "failed"
    assert result["error"]["code"] == "service_error"
    assert "private" not in str(result)


def test_cwd_diagnostic_is_also_inside_the_output_limit():
    process = SimpleNamespace(
        stdout=io.StringIO("okay"),
        stderr=io.StringIO("failed to find initial working directory '/missing'"),
        wait=Mock(return_value=128),
    )
    sandbox = SimpleNamespace(exec=Mock(return_value=process))
    service = Service(registry=object())
    service.running = Mock(return_value=({"config": {"workdir": "/missing"}}, sandbox))
    result = service.execute("dev", ["true"], capture=True, max_output_bytes=64)
    assert result["exit_code"] == 128 and result["stderr_truncated"]
    assert len(result["stderr"].encode()) == 64
    assert result["stdout"] == "okay" and not result["stdout_truncated"]
    sandbox.exec.assert_called_once()


def test_sdk_calls_without_request_id_use_the_function_call_identity():
    runs = Runs()
    execute = Mock(return_value=completed())
    factory = Mock(return_value=SimpleNamespace(execute=execute))
    req = request(request_id=None)
    execute_claimed(req, "fc-stable-call", runs, factory)
    execute_claimed(req, "fc-stable-call", runs, factory)
    assert execute.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"command": []},
        {"command": [""]},
        {"command": ["pwd", "\0"]},
        {"command": ["echo", 1]},
        {"command": ["echo", "x" * 65536]},
        {"name": "../dev"},
        {"cwd": "relative"},
        {"cwd": "/a/../b"},
        {"cwd": "/a\0"},
        {"timeout": True},
        {"timeout": 0},
        {"timeout": 3601},
        {"request_id": "short"},
        {"unknown_option": True},
    ],
)
def test_invalid_execution_requests_are_rejected(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_capture_limit_drains_discarded_output_and_handles_utf8():
    consumed = []

    def chunks():
        for text in ["é", "é", "done"]:
            consumed.append(text)
            yield text

    value, truncated = Service._capture(chunks(), 3)
    assert value == "é" and truncated
    assert len(value.encode("utf-8")) <= 3
    assert consumed == ["é", "é", "done"]
    assert Service._capture(io.StringIO("okay"), 4) == ("okay", False)
    assert Service._capture(io.StringIO(""), 4) == ("", False)


@pytest.mark.parametrize("limit,capture", [(0, True), (True, True), (8, False)])
def test_invalid_capture_limit_fails_before_provider_access(limit, capture):
    service = Service(registry=object())
    with pytest.raises(ValueError, match="max_output_bytes"):
        service.execute("dev", ["true"], capture=capture, max_output_bytes=limit)
