"""Credentials travel only over runtime stdin, and never overwrite refreshed auth."""

import io
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from box import agents
from box.environment import ASSETS, build_image


def test_pi_auth_is_read_from_client_home_without_serializing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".pi/agent/auth.json"
    path.parent.mkdir(parents=True)
    contents = b'{"provider":{"type":"api_key","key":"test-secret"}}\n'
    path.write_bytes(contents)
    assert agents.read_pi_auth() == contents
    sb = MagicMock()
    sb.exec.return_value.wait.return_value = 0
    agents.install_pi_auth(sb)
    args, kwargs = sb.exec.call_args
    assert "test-secret" not in repr((args, kwargs))
    assert "env" not in kwargs and "secrets" not in kwargs
    sb.exec.return_value.stdin.write.assert_called_once_with(contents)
    sb.exec.return_value.stdin.write_eof.assert_called_once()
    sb.exec.return_value.stdin.drain.assert_called_once()


@pytest.mark.parametrize("content", [None, "bad secret-value JSON", '"secret-value"', "[]"])
def test_missing_or_invalid_auth_has_a_safe_error(tmp_path, monkeypatch, content):
    monkeypatch.setenv("HOME", str(tmp_path))
    if content is not None:
        path = tmp_path / ".pi/agent/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(content)
    with pytest.raises(ValueError, match="authenticate Pi") as failure:
        agents.read_pi_auth()
    assert "secret-value" not in str(failure.value)


def test_runtime_installer_is_atomic_private_and_does_not_replace_refreshed_auth(tmp_path):
    path = tmp_path / ".pi/agent/auth.json"

    def install(data):
        return subprocess.run(
            [sys.executable, "-c", agents.INSTALL_PI_AUTH, str(path)],
            input=data,
            capture_output=True,
        )

    assert install(b'{"key":"original"}').returncode == 0
    assert json.loads(path.read_text()) == {"key": "original"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.write_text('{"key":"refreshed"}')
    assert install(b'{"key":"client"}').returncode == 0
    assert json.loads(path.read_text()) == {"key": "refreshed"}
    assert list(path.parent.iterdir()) == [path]
    path.unlink()
    other = tmp_path / "keep"
    other.write_text("do not change")
    path.symlink_to(other)
    assert install(b"{}").returncode != 0
    assert other.read_text() == "do not change"
    assert not list(path.parent.glob(".auth-*"))


def test_transfer_failure_never_echoes_provider_output(monkeypatch):
    monkeypatch.setattr(agents, "read_pi_auth", lambda: b"{}")
    sb = MagicMock()
    sb.exec.return_value = SimpleNamespace(
        stdin=MagicMock(),
        stderr=io.StringIO("private value"),
        wait=lambda: 1,
    )
    with pytest.raises(ValueError, match="not confirmed") as failure:
        agents.install_pi_auth(sb)
    assert "private" not in str(failure.value)


def test_image_only_uploads_repository_assets(monkeypatch):
    calls = []

    class Image:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
                return self

            return call

    monkeypatch.setattr("box.environment.modal.Image.debian_slim", lambda **kwargs: Image())
    build_image()
    for name, args, kwargs in calls:
        if name in {"add_local_dir", "add_local_file"}:
            assert args[0].is_relative_to(ASSETS)
            assert args[0].name != "auth.json"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in repr((args, kwargs))
    assert not list(ASSETS.rglob("auth.json"))
    assert not list(ASSETS.rglob(".credentials.json"))


@pytest.mark.parametrize("token", ["a'$(echo unsafe)\nline", "", None])
def test_claude_token_is_quoted_refreshed_and_unset_for_shells(tmp_path, monkeypatch, token):
    import importlib.util
    import shutil

    spec = importlib.util.spec_from_file_location("agent_ssh_env", ASSETS / "ssh_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    module.write_environment(tmp_path)
    path = tmp_path / "ssh-env.sh"
    process = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            '. "$1"; printf %s "${CLAUDE_CODE_OAUTH_TOKEN-unset}"',
            "bash",
            str(path),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": "stale", "BASH_ENV": ""},
    )
    assert process.returncode == 0
    assert process.stdout == (token or "unset")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
