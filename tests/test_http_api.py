"""HTTP contracts; authentication at Modal's proxy is tested against deployment."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import modal
import pytest

from box.http_api import create_api
from box.remote_exec import failure


class LocalClient:
    """Exercise ASGI directly without sockets or version-specific test clients."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)


@pytest.fixture
def http():
    result = {
        "status": "completed",
        "request_id": "request-123",
        "call_id": "fc-test",
        "exit_code": 23,
        "stdout": "out\n",
        "stderr": "err\n",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    remote = AsyncMock(return_value=result)
    spawn = AsyncMock(return_value=SimpleNamespace(object_id="fc-test"))
    function = SimpleNamespace(remote=SimpleNamespace(aio=remote), spawn=SimpleNamespace(aio=spawn))
    runs = SimpleNamespace(
        put=SimpleNamespace(aio=AsyncMock()),
        get=SimpleNamespace(aio=AsyncMock(return_value={"request_id": "request-123"})),
    )
    get = AsyncMock(return_value=result)
    calls = Mock(return_value=SimpleNamespace(get=SimpleNamespace(aio=get)))
    identity = Mock(return_value="gateway-request-123")
    client = LocalClient(create_api(function, runs, calls, identity))
    return SimpleNamespace(
        client=client,
        remote=remote,
        result=result,
        function=function,
        runs=runs,
        spawn=spawn,
        get=get,
        calls=calls,
        identity=identity,
    )


def test_exec_returns_command_exit_without_conflating_it_with_http_failure(http):
    payload = {"name": "dev", "command": ["false"], "cwd": "/", "request_id": "request-123"}
    response = http.client.post("/exec", json=payload)
    assert response.status_code == 200
    assert response.json() == http.result
    http.remote.assert_awaited_once_with({**payload, "timeout": 60})
    http.identity.assert_not_called()


def test_gateway_without_stable_identity_fails_before_dispatch(http):
    http.identity.return_value = None
    with pytest.raises(RuntimeError, match="stable Modal request identity"):
        http.client.post("/exec", json={"name": "dev", "command": ["pwd"]})
    http.remote.assert_not_awaited()


def test_gateway_assigns_request_id_before_remote_dispatch(http):
    response = http.client.post("/exec", json={"name": "dev", "command": ["pwd"]})
    assert response.status_code == 200
    payload = http.remote.call_args.args[0]
    assert payload["request_id"] == "gateway-request-123"
    # A replay of this same Modal HTTP input must use the same dispatch identity.
    http.client.post("/exec", json={"name": "dev", "command": ["pwd"]})
    assert http.remote.call_args.args[0]["request_id"] == payload["request_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "dev", "command": []},
        {"name": "dev", "command": ["echo", "secret-argument"], "cwd": "relative"},
        {"name": "dev", "command": ["pwd"], "timeout": True},
        {"name": "dev", "command": ["pwd"], "timeout": 91},
        {"name": "dev", "command": ["pwd"], "unexpected": "secret-argument"},
    ],
)
def test_validation_precedes_dispatch_and_does_not_echo_arguments(http, payload):
    response = http.client.post("/exec", json=payload)
    assert response.status_code == 422
    assert "secret-argument" not in response.text
    http.remote.assert_not_awaited()


def test_operational_worker_failure_is_not_a_successful_command(http):
    http.remote.return_value = failure("request-123", "fc-test", "box_unavailable", "Paused", 409)
    response = http.client.post("/exec", json={"name": "dev", "command": ["pwd"]})
    assert response.status_code == 409
    assert response.json()["status"] == "failed"


@pytest.mark.parametrize(
    "error,status",
    [
        (modal.exception.FunctionTimeoutError(), 504),
        (RuntimeError("private credential diagnostic"), 503),
    ],
)
def test_dispatch_error_is_sanitized_and_never_retried(http, error, status):
    http.remote.side_effect = error
    response = http.client.post("/exec", json={"name": "dev", "command": ["pwd"]})
    assert response.status_code == status
    assert "private credential" not in response.text
    assert http.remote.await_count == 1


def test_spawn_accepts_long_timeout_and_returns_owned_call_handle(http):
    request = {
        "name": "dev",
        "command": ["sleep", "100"],
        "timeout": 1200,
        "request_id": "request-123",
    }
    response = http.client.post("/spawn", json=request)
    assert response.status_code == 202
    assert response.json()["call_id"] == "fc-test"
    assert response.headers["location"] == "/poll/fc-test"
    http.spawn.assert_awaited_once_with({**request, "cwd": None})
    http.runs.put.aio.assert_awaited_once_with("call:fc-test", {"request_id": "request-123"})
    http.remote.assert_not_awaited()


def test_spawn_does_not_hide_uncertain_submission_or_retry(http):
    http.spawn.side_effect = OSError("private provider detail")
    response = http.client.post("/spawn", json={"name": "dev", "command": ["true"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dispatch_unconfirmed"
    assert "private provider" not in response.text
    assert http.spawn.await_count == 1


def test_poll_is_nonblocking_repeatable_and_never_spawns(http):
    http.get.side_effect = [TimeoutError(), http.result, http.result]
    first = http.client.get("/poll/fc-test")
    assert first.status_code == 202 and first.json()["status"] == "pending"
    for _ in range(2):
        response = http.client.get("/poll/fc-test")
        assert response.status_code == 200
        assert response.json()["result"] == http.result
    assert all(call.kwargs == {"timeout": 0} for call in http.get.call_args_list)
    http.spawn.assert_not_awaited()
    http.remote.assert_not_awaited()


def test_unknown_call_is_rejected_before_modal_result_access(http):
    http.runs.get.aio.return_value = None
    response = http.client.get("/poll/fc-unrelated")
    assert response.status_code == 404
    http.calls.assert_not_called()


def test_ownership_lookup_timeout_is_not_misreported_as_pending(http):
    http.runs.get.aio.side_effect = TimeoutError()
    response = http.client.get("/poll/fc-test")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "poll_error"
    http.calls.assert_not_called()


@pytest.mark.parametrize(
    "error,status",
    [
        (modal.exception.OutputExpiredError(), 410),
        (modal.exception.NotFoundError("missing"), 404),
        (modal.exception.FunctionTimeoutError(), 504),
        (RuntimeError("private traceback"), 503),
    ],
)
def test_poll_distinguishes_expiry_failure_and_pending(http, error, status):
    http.get.side_effect = error
    response = http.client.get("/poll/fc-test")
    assert response.status_code == status
    assert "private traceback" not in response.text
    assert response.json()["status"] != "pending"


def test_remote_worker_failure_is_terminal_not_an_endless_poll_retry(http):
    http.get.side_effect = modal.exception.RemoteError("private worker diagnostic")
    response = http.client.get("/poll/fc-test")
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["result"]["error"]["code"] == "worker_failed"
    assert "private worker diagnostic" not in response.text


def test_poll_can_return_a_failed_execution_as_a_terminal_result(http):
    http.get.return_value = failure("request-123", "fc-test", "box_unavailable", "Paused", 409)
    response = http.client.get("/poll/fc-test")
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["result"]["error"]["code"] == "box_unavailable"
