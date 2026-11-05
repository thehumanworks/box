"""Credential provisioning must be private, reusable and scope-aware."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_remote.py"
spec = importlib.util.spec_from_file_location("deploy_remote", SCRIPT)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def workspace(scoped=False):
    token = SimpleNamespace(token_id="wk-test", token_secret="test-secret-not-real")
    manager = SimpleNamespace(
        create=Mock(return_value=token),
        list=Mock(return_value=[SimpleNamespace(token_id=token.token_id, scoped=scoped)]),
        allow=Mock(),
        delete=Mock(),
    )
    # Workspace.from_context() has a name, but no hydrated object_id in SDK 1.6.3.
    return SimpleNamespace(name="test-workspace", proxy_tokens=manager)


@pytest.mark.parametrize("scoped", [False, True])
def test_proxy_token_is_created_once_and_only_scoped_tokens_get_environment_grants(
    tmp_path, scoped
):
    ws = workspace(scoped)
    directory = tmp_path / "private"
    path = deploy.proxy_credentials(ws, "main", directory)
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    value = json.loads(path.read_text())
    assert value["workspace"] == ws.name
    assert value["environment"] == "main"
    assert value["token_secret"] == "test-secret-not-real"
    assert deploy.proxy_credentials(ws, "main", directory) == path
    ws.proxy_tokens.create.assert_called_once()
    assert ws.proxy_tokens.allow.call_count == (2 if scoped else 0)


def test_refuses_readable_or_symlinked_credentials(tmp_path):
    ws = workspace()
    path = deploy.proxy_credentials(ws, "main", tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        deploy.proxy_credentials(ws, "main", tmp_path)
    path.unlink()
    target = tmp_path / "other"
    target.write_text("not credentials")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        deploy.proxy_credentials(ws, "main", tmp_path)
    assert ws.proxy_tokens.create.call_count == 1


def test_unknown_or_revoked_token_is_not_silently_replaced(tmp_path):
    ws = workspace()
    deploy.proxy_credentials(ws, "main", tmp_path)
    ws.proxy_tokens.list.return_value = []
    with pytest.raises(ValueError, match="revoked"):
        deploy.proxy_credentials(ws, "main", tmp_path)
    assert ws.proxy_tokens.create.call_count == 1


def test_known_new_token_is_revoked_if_storing_it_fails(tmp_path, monkeypatch):
    ws = workspace()
    monkeypatch.setattr(deploy.os, "fsync", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        deploy.proxy_credentials(ws, "main", tmp_path)
    ws.proxy_tokens.delete.assert_called_once_with("wk-test")
    assert not (tmp_path / "proxy.json").exists()


def test_uncertain_creation_reservation_prevents_minting_another_token(tmp_path):
    ws = workspace()
    ws.proxy_tokens.create.side_effect = OSError("response lost")
    with pytest.raises(OSError):
        deploy.proxy_credentials(ws, "main", tmp_path)
    with pytest.raises(ValueError):
        deploy.proxy_credentials(ws, "main", tmp_path)
    assert ws.proxy_tokens.create.call_count == 1
    ws.proxy_tokens.delete.assert_not_called()
