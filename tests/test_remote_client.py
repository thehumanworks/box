"""Offline HTTPS transport/configuration tests; CLI smoke tests use the real service."""

import io
import json
import ssl
from http.client import IncompleteRead
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from box import remote_client as remote


def completed(code=0):
    return {
        "status": "completed",
        "exit_code": code,
        "stdout": "out",
        "stderr": "err",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "request_id": "request-123",
        "call_id": "fc-test",
    }


class Response(io.BytesIO):
    def __init__(self, value, code=200):
        super().__init__(json.dumps(value).encode())
        self.code = code


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    folder = tmp_path / "box/box-exec-api/test/main"
    folder.mkdir(parents=True)
    credentials = folder / "proxy.json"
    credentials.write_text(
        json.dumps(
            {
                "workspace": "test",
                "environment": "main",
                "token_id": "wk-test",
                "token_secret": "ws-private",
            }
        )
    )
    credentials.chmod(0o600)
    manifest = folder / "deployment.json"
    manifest.write_text(
        json.dumps(
            {
                "workspace": "test",
                "environment": "main",
                "app": "box-exec-api",
                "credentials_file": "proxy.json",
                "url": "https://test--box-exec-api-web.us-east.modal.run",
            }
        )
    )
    return manifest, credentials


@pytest.fixture
def http(deployment, monkeypatch):
    opener = Mock()
    opener.open.return_value = Response(completed())
    build = Mock(return_value=opener)
    monkeypatch.setattr(remote, "build_opener", build)
    return remote.RemoteClient(), opener, build


def update(path, **changes):
    value = json.loads(path.read_text())
    path.write_text(json.dumps({**value, **changes}))


def test_auto_discovery_requires_one_deployment(deployment):
    manifest, _ = deployment
    assert remote.deployment_file() == manifest
    other = manifest.parent.parent / "other/deployment.json"
    other.parent.mkdir()
    other.write_text("{}")
    with pytest.raises(remote.RemoteClientError, match="Multiple"):
        remote.deployment_file()
    assert remote.deployment_file(manifest) == manifest
    other.unlink()
    manifest.unlink()
    with pytest.raises(remote.RemoteClientError, match="No remote deployment"):
        remote.deployment_file()


@pytest.mark.parametrize(
    "url",
    [
        "http://test--box-exec-api-web.modal.run",
        "https://attacker.invalid",
        "https://test--box-exec-api-web.modal.run.attacker.invalid",
        "https://other--box-exec-api-web.modal.run",
        "https://user:password@test--box-exec-api-web.modal.run",
        "https://test--box-exec-api-web.modal.run?token=bad",
        "https://test--box-exec-api-web.modal.run/path",
        "https://test--box-exec-api-web.modal.run:8080",
    ],
)
def test_refuses_untrusted_or_non_https_destinations_before_network(deployment, monkeypatch, url):
    update(deployment[0], url=url)
    monkeypatch.setattr(remote, "build_opener", Mock(side_effect=AssertionError("offline")))
    with pytest.raises(remote.RemoteClientError, match="HTTPS Modal URL"):
        remote.RemoteClient()


@pytest.mark.parametrize("kind", ["public", "symlink", "wrong_workspace", "api_token", "bad_json"])
def test_credentials_are_private_and_bound_to_the_manifest(deployment, kind):
    _, credentials = deployment
    if kind == "public":
        credentials.chmod(0o644)
    elif kind == "symlink":
        target = credentials.with_suffix(".backup")
        credentials.rename(target)
        credentials.symlink_to(target)
    elif kind == "wrong_workspace":
        update(credentials, workspace="other")
    elif kind == "api_token":
        update(credentials, token_id="ak-private", token_secret="as-private")
    else:
        credentials.write_text("invalid secret file ws-private")
    with pytest.raises(remote.RemoteClientError) as error:
        remote.RemoteClient()
    assert "ws-private" not in str(error.value)
    assert "as-private" not in str(error.value)


def test_exec_uses_verified_https_literal_argv_and_a_stable_client_request_id(http):
    client, opener, build = http
    value = client.execute(
        "dev", ["echo", "a b", "$(false)", ""], cwd="/", request_id="request-123"
    )
    assert value["status"] == "completed"
    req = opener.open.call_args.args[0]
    assert req.method == "POST" and req.full_url.endswith("/exec")
    assert json.loads(req.data) == {
        "name": "dev",
        "command": ["echo", "a b", "$(false)", ""],
        "cwd": "/",
        "timeout": 60,
        "request_id": "request-123",
    }
    assert req.get_header("Modal-key") == "wk-test"
    assert req.get_header("Modal-secret") == "ws-private"
    redirect, https = build.call_args.args
    assert isinstance(redirect, remote.NoRedirect)
    assert https._context.verify_mode == ssl.CERT_REQUIRED
    assert https._context.check_hostname
    assert redirect.redirect_request(None, None, 303, "", {}, "https://attacker.invalid") is None


@pytest.mark.parametrize(
    "error",
    [
        URLError("ws-private"),
        TimeoutError(),
        IncompleteRead(b"ws-private"),
        KeyboardInterrupt(),
    ],
)
def test_uncertain_post_exposes_generated_retry_id_without_retry_or_secret_leak(http, error):
    client, opener, _ = http
    opener.open.side_effect = error
    with pytest.raises(remote.RemoteClientError) as caught:
        client.spawn("dev", ["sleep", "3"])
    assert opener.open.call_count == 1
    sent = json.loads(opener.open.call_args.args[0].data)
    assert len(sent["request_id"]) == 32
    assert caught.value.result()["request_id"] == sent["request_id"]
    assert "--request-id " + sent["request_id"] in str(caught.value)
    assert "ws-private" not in str(caught.value)
    assert caught.value.exit_code == (130 if isinstance(error, KeyboardInterrupt) else 1)


def test_spawn_and_poll_use_the_right_routes_and_do_not_follow_poll_urls(http):
    client, opener, _ = http
    opener.open.return_value = Response({"status": "submitted", "call_id": "fc-test"}, 202)
    assert client.spawn("dev", ["true"])["call_id"] == "fc-test"
    sent = json.loads(opener.open.call_args.args[0].data)
    assert sent["timeout"] == 3600
    assert opener.open.call_args.args[0].full_url.endswith("/spawn")
    opener.open.return_value = Response({"status": "pending", "call_id": "fc-test"}, 202)
    assert client.poll("fc-test")["status"] == "pending"
    req = opener.open.call_args.args[0]
    assert req.method == "GET" and req.full_url.endswith("/poll/fc-test")
    assert req.data is None


@pytest.mark.parametrize(
    "body,status",
    [
        (b"ws-private", 401),
        (b"ws-private", 403),
        (b"<html>ws-private</html>", 502),
        (b'{"status":"completed"}', 200),
        (b"{}", 303),
    ],
)
def test_http_errors_invalid_json_and_redirects_are_never_success_or_secret_leaks(
    http, body, status
):
    client, opener, _ = http
    opener.open.side_effect = HTTPError(client.url, status, "ws-private", {}, io.BytesIO(body))
    with pytest.raises(remote.RemoteClientError) as caught:
        client.execute("dev", ["true"], request_id="request-123")
    assert "ws-private" not in str(caught.value)
    assert opener.open.call_count == 1


def test_structured_service_failure_and_expired_poll_are_preserved(http):
    client, opener, _ = http
    failure = {"status": "failed", "error": {"code": "exec_timeout", "message": "ws-private"}}
    opener.open.return_value = Response(failure, 504)
    result = client.execute("dev", ["true"], request_id="request-123")
    assert result["error"]["message"] == "[redacted]"
    assert result["request_id"] == "request-123"
    opener.open.return_value = Response({"status": "expired", "call_id": "fc-test"}, 410)
    assert client.poll("fc-test")["status"] == "expired"


@pytest.mark.parametrize(
    "changes",
    [
        {"command": []},
        {"command": [""]},
        {"command": ["echo", "\0"]},
        {"command": ["echo", "x" * 65536]},
        {"timeout": 91},
        {"timeout": True},
        {"cwd": "/a/../b"},
        {"request_id": "bad"},
    ],
)
def test_invalid_arguments_never_reach_network(http, changes):
    client, opener, _ = http
    kwargs = {"name": "dev", "command": ["true"], **changes}
    with pytest.raises(ValueError):
        client.execute(**kwargs)
    opener.open.assert_not_called()


def test_nested_poll_error_redacts_credentials_without_losing_call_id(http):
    client, opener, _ = http
    failure = {"status": "failed", "error": {"code": "worker_failed", "message": "ws-private"}}
    opener.open.return_value = Response(
        {"status": "failed", "call_id": "fc-test", "result": failure}
    )
    value = client.poll("fc-test")
    assert value["result"]["error"]["message"] == "[redacted]"
    assert value["result"]["call_id"] == "fc-test"


def test_invalid_call_id_cannot_be_used_as_a_url(http):
    client, opener, _ = http
    with pytest.raises(ValueError):
        client.poll("fc-a/../../exec")
    opener.open.assert_not_called()


def test_response_size_is_bounded(http, monkeypatch):
    client, opener, _ = http
    monkeypatch.setattr(remote, "MAX_RESPONSE_BYTES", 8)
    with pytest.raises(remote.RemoteClientError, match="oversized"):
        client.execute("dev", ["true"])


def test_client_limits_match_the_server_contract():
    from box import remote_exec

    assert remote.SYNC_TIMEOUT == remote_exec.SYNC_TIMEOUT
    assert remote.MAX_TIMEOUT == remote_exec.MAX_TIMEOUT
