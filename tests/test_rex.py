"""Rex startup contracts, with network state supplied by a local fake server."""

import importlib.util
import os
import subprocess
from unittest.mock import patch

import pytest

from box.environment import ASSETS

spec = importlib.util.spec_from_file_location("box_rex_network", ASSETS / "rex_network.py")
rex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rex)

HOSTNAME = "rex-box-test.example.ts.net"
READY = {
    "rex_url": f"https://{HOSTNAME}",
    "rex_hostname": HOSTNAME,
    "rex_host_id": "saved-rex-host-id",
}
ONLINE = {
    "host_id": READY["rex_host_id"],
    "tailscale": {
        "state": "running",
        "dns_name": HOSTNAME,
        "account": {"login_name": "test-owner@example.com"},
        "allow_tailnet_members": False,
        "https": {"state": "available", "url": READY["rex_url"]},
    },
}


def state(**changes):
    return {**ONLINE, "tailscale": {**ONLINE["tailscale"], **changes}}


def statuses(*values):
    """Allow polling to continue at the last supplied server state."""
    pending = iter(values)
    current = values[-1]

    def next_status():
        nonlocal current
        current = next(pending, current)
        return current

    return next_status


def test_auth_key_uses_stdin_and_fixed_local_socket():
    secret_names = (
        "OP_SERVICE_ACCOUNT_TOKEN",
        "TAILSCALE_AUTHKEY",
        "TAILSCALE_AUTH_KEY",
        "TS_AUTHKEY",
    )
    with (
        patch.dict(os.environ, dict.fromkeys(secret_names, "environment-secret")),
        patch.object(
            rex.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")
        ) as run,
    ):
        rex.command("server", "tailscale", "on", "--auth-key-file=-", auth_key="secret-value")
    assert run.call_args.kwargs["input"] == "secret-value\n"
    argv = run.call_args.args[0]
    assert "unix:///run/box/rex/server.sock" in argv
    assert "--autostart=false" in argv
    assert "secret-value" not in str(argv)
    assert not set(secret_names).intersection(run.call_args.kwargs["env"])


def test_command_failure_does_not_expose_credentials():
    with patch.object(
        rex.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 1, "secret-value", "bad secret-value"),
    ):
        with pytest.raises(RuntimeError) as error:
            rex.command("server", "tailscale", "on", auth_key="secret-value")
    assert "secret-value" not in str(error.value)


def test_saved_online_identity_does_not_reenroll():
    with (
        patch.object(rex, "status", return_value=ONLINE),
        patch.object(rex, "command") as command,
    ):
        assert rex.connect("rex-box-test", auth_key="unused-key") == READY
    for call in command.call_args_list:
        assert not call.kwargs.get("auth_key")
        assert "logout" not in call.args
        assert "--auth-key-file=-" not in call.args


def test_saved_disabled_identity_resumes_without_credentials():
    with (
        patch.object(rex, "status", side_effect=statuses(state(state="disabled"), ONLINE)),
        patch.object(rex, "command") as command,
    ):
        assert rex.connect("rex-box-test") == READY
    assert any("on" in call.args for call in command.call_args_list)
    assert all(not call.kwargs.get("auth_key") for call in command.call_args_list)


def test_first_enrollment_passes_key_only_to_enable():
    with (
        patch.object(rex, "status", side_effect=statuses(state(state="disabled"), ONLINE)),
        patch.object(rex, "command") as command,
    ):
        assert rex.connect("rex-box-test", auth_key="secret-value") == READY
    enrollment = [call for call in command.call_args_list if call.kwargs.get("auth_key")]
    assert len(enrollment) == 1
    assert "on" in enrollment[0].args
    assert enrollment[0].kwargs["auth_key"] == "secret-value"
    assert "secret-value" not in str(enrollment[0].args)


def test_missing_login_disables_node_so_credential_retry_can_enroll():
    with (
        patch.object(rex, "status", return_value=state(state="needs_login")),
        patch.object(rex, "command") as command,
    ):
        with pytest.raises(rex.LoginRequired):
            rex.connect("rex-box-test")
    assert any("off" in call.args for call in command.call_args_list)


def test_retry_with_key_disables_unenrolled_node_before_enabling():
    with (
        patch.object(rex, "status", side_effect=statuses(state(state="needs_login"), ONLINE)),
        patch.object(rex, "command") as command,
    ):
        assert rex.connect("rex-box-test", auth_key="secret-value") == READY
    assert "off" in command.call_args_list[0].args
    assert "on" in command.call_args_list[1].args
    assert command.call_args_list[1].kwargs["auth_key"] == "secret-value"


def test_key_enrollment_waits_for_asynchronous_login():
    with (
        patch.object(
            rex,
            "status",
            side_effect=statuses(state(state="disabled"), state(state="needs_login"), ONLINE),
        ),
        patch.object(rex, "command") as command,
        patch.object(rex.time, "sleep") as sleep,
    ):
        assert rex.connect("rex-box-test", auth_key="secret-value") == READY
    assert sleep.called
    assert not any("off" in call.args for call in command.call_args_list)


def test_machine_approval_is_reported():
    with (
        patch.object(rex, "status", return_value=state(state="needs_machine_approval")),
        patch.object(rex, "command"),
    ):
        with pytest.raises(RuntimeError, match="requires approval"):
            rex.connect("rex-box-test")


def test_server_readiness_has_a_deadline():
    with (
        patch.object(rex, "status", return_value={}),
        patch.object(rex.time, "monotonic", side_effect=[0, 0, 26]),
        patch.object(rex.time, "sleep"),
    ):
        with pytest.raises(RuntimeError, match="server did not become ready"):
            rex.connect("rex-box-test")


def test_https_readiness_has_a_deadline():
    with (
        patch.object(rex, "status", return_value=state(https={"state": "starting"})),
        patch.object(rex, "command"),
        patch.object(rex.time, "monotonic", side_effect=[0, 0, 0, 0, 66]),
        patch.object(rex.time, "sleep"),
    ):
        with pytest.raises(RuntimeError, match="HTTPS.*did not become ready"):
            rex.connect("rex-box-test")


def test_waits_for_https_to_become_available():
    pending = state(https={"state": "starting"})
    with (
        patch.object(rex, "status", side_effect=statuses(pending, pending, ONLINE)),
        patch.object(rex, "command"),
        patch.object(rex.time, "sleep") as sleep,
    ):
        assert rex.connect("rex-box-test") == READY
    assert sleep.called


@pytest.mark.parametrize(
    "url",
    ["http://" + HOSTNAME, "https://other.example.ts.net", "https://user:secret@" + HOSTNAME],
)
def test_rejects_invalid_https_address(url):
    with (
        patch.object(rex, "status", return_value=state(https={"state": "available", "url": url})),
        patch.object(rex, "command"),
    ):
        with pytest.raises(RuntimeError):
            rex.connect("rex-box-test")


def test_waits_for_owner_after_https_is_available():
    pending = state(account=None)
    with (
        patch.object(rex, "status", side_effect=statuses(pending, pending, ONLINE)),
        patch.object(rex, "command"),
        patch.object(rex.time, "sleep") as sleep,
    ):
        assert rex.connect("rex-box-test") == READY
    sleep.assert_called_once_with(0.5)


@pytest.mark.parametrize("account", [None, {}, {"login_name": ""}])
def test_missing_owner_never_reports_readiness_and_times_out(account):
    with (
        patch.object(rex, "status", return_value=state(account=account)),
        patch.object(rex, "command"),
        patch.object(rex.time, "monotonic", side_effect=[0, 0, 0, 0, 66]),
        patch.object(rex.time, "sleep") as sleep,
    ):
        with pytest.raises(RuntimeError, match="owner identity"):
            rex.connect("rex-box-test")
    sleep.assert_called_once_with(0.5)


def test_tagged_node_is_rejected_even_while_owner_is_loading():
    with (
        patch.object(rex, "status", return_value=state(account=None, tags=["tag:server"])),
        patch.object(rex, "command"),
    ):
        with pytest.raises(RuntimeError, match="untagged"):
            rex.connect("rex-box-test")


def test_rejects_tailnet_wide_access():
    with (
        patch.object(rex, "status", return_value=state(allow_tailnet_members=True)),
        patch.object(rex, "command"),
    ):
        with pytest.raises(RuntimeError, match="access policy"):
            rex.connect("rex-box-test")


@pytest.mark.parametrize(
    "error", [RuntimeError("socket not ready"), subprocess.TimeoutExpired("rex", 20)]
)
def test_transient_status_failure_can_be_polled(error):
    with patch.object(rex, "command", side_effect=error):
        assert rex.status() == {}
