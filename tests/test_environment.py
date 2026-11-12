import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from box.environment import ASSETS, ENTRYPOINT, start_network

spec = importlib.util.spec_from_file_location("box_network", ASSETS / "network.py")
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)
ssh_spec = importlib.util.spec_from_file_location("box_ssh_env", ASSETS / "ssh_env.py")
ssh_env = importlib.util.module_from_spec(ssh_spec)
ssh_spec.loader.exec_module(ssh_env)

ONLINE = {
    "BackendState": "Running",
    "Self": {"Online": True},
    "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.90.80.70"],
}
READY = {"host": "100.90.80.70", "tailscale_ip": "100.90.80.70", "hostname": "box-test"}


class NetworkStartupTests(unittest.TestCase):
    def test_saved_identity_resumes_without_credentials(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(network, "status", return_value=ONLINE),
            patch.object(network, "command") as command,
        ):
            self.assertEqual(network.connect("box-test"), READY)
        self.assertEqual(command.call_args.args[0], "set")
        self.assertNotIn("up", command.call_args.args)

    def test_first_login_sends_key_via_stdin(self):
        with (
            patch.dict(os.environ, {"TAILSCALE_AUTHKEY": "secret-value"}, clear=True),
            patch.object(network, "status", side_effect=[{"BackendState": "NeedsLogin"}, ONLINE]),
            patch.object(network, "command") as command,
        ):
            self.assertEqual(network.connect("box-test"), READY)
            self.assertNotIn("TAILSCALE_AUTHKEY", os.environ)
        self.assertIn("--auth-key=file:/dev/stdin", command.call_args.args)
        self.assertNotIn("secret-value", command.call_args.args)
        self.assertEqual(command.call_args.kwargs["auth_key"], "secret-value")

    def test_fresh_identity_needs_credentials(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(network, "status", return_value={"BackendState": "NeedsLogin"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "login required"):
                network.connect("box-test")

    def test_stopped_saved_identity_does_not_require_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(network, "status", side_effect=[{"BackendState": "Stopped"}, ONLINE]),
            patch.object(network, "command") as command,
        ):
            self.assertEqual(network.connect("box-test"), READY)
        self.assertEqual(command.call_args.args[0], "up")
        self.assertNotIn("--auth-key=file:/dev/stdin", command.call_args.args)

    def test_machine_approval_is_reported(self):
        with patch.object(network, "status", return_value={"BackendState": "NeedsMachineAuth"}):
            with self.assertRaisesRegex(RuntimeError, "requires approval"):
                network.connect("box-test")

    def test_daemon_readiness_has_deadline(self):
        with (
            patch.object(network, "status", return_value={}),
            patch.object(network.time, "monotonic", side_effect=[0, 0, 26]),
            patch.object(network.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "daemon did not become ready"):
                network.connect("box-test")

    def test_waits_for_online_status(self):
        offline = {**ONLINE, "Self": {"Online": False}}
        with (
            patch.object(network, "status", side_effect=[ONLINE, offline, ONLINE]),
            patch.object(network, "command"),
            patch.object(network.time, "sleep") as sleep,
        ):
            self.assertEqual(network.connect("box-test"), READY)
        sleep.assert_called_once_with(0.5)

    def test_auth_key_is_stdin_and_failure_does_not_echo_it(self):
        result = subprocess.CompletedProcess([], 1, "", "failed with secret-value")
        with patch.object(network.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "Tailscale up failed") as error:
                network.command("up", "--auth-key=file:/dev/stdin", auth_key="secret-value")
        self.assertEqual(run.call_args.kwargs["input"], "secret-value\n")
        self.assertNotIn("secret-value", str(run.call_args.args))
        self.assertNotIn("secret-value", str(error.exception))


class NetworkClientTests(unittest.TestCase):
    def sandbox(self, output=None, error="", exit_code=0):
        sb = MagicMock()
        process = sb.exec.return_value
        process.stdout = io.StringIO(json.dumps(READY) if output is None else output)
        process.stderr = io.StringIO(error)
        process.wait.return_value = exit_code
        return sb

    def test_exec_secret_is_attached_only_at_startup(self):
        sb = self.sandbox()
        sb.exec.side_effect = [
            self.sandbox(error="login required", exit_code=78).exec.return_value,
            sb.exec.return_value,
        ]
        with patch("box.environment.modal.Secret.from_name", return_value="secret"):
            self.assertEqual(start_network(sb, "box-test", secret_name="tailscale"), READY)
        self.assertEqual(sb.exec.call_args.kwargs["secrets"], ["secret"])
        self.assertNotIn("env", sb.exec.call_args.kwargs)

    def test_auth_key_has_priority_and_is_not_a_command_argument(self):
        sb = self.sandbox()
        sb.exec.side_effect = [
            self.sandbox(error="login required", exit_code=78).exec.return_value,
            sb.exec.return_value,
        ]
        start_network(sb, "box-test", auth_key="secret-value", secret_name="tailscale")
        self.assertEqual(sb.exec.call_args.kwargs["env"], {"TAILSCALE_AUTHKEY": "secret-value"})
        self.assertNotIn("secrets", sb.exec.call_args.kwargs)
        self.assertNotIn("secret-value", str(sb.exec.call_args.args))

    def test_resume_does_not_require_a_secret(self):
        sb = self.sandbox()
        with patch("box.environment.modal.Secret.from_name") as secret:
            self.assertEqual(start_network(sb, "box-test", secret_name="deleted-secret"), READY)
        secret.assert_not_called()
        sb.exec.assert_called_once()
        self.assertNotIn("env", sb.exec.call_args.kwargs)
        self.assertNotIn("secrets", sb.exec.call_args.kwargs)

    def test_rex_metadata_survives_network_startup(self):
        ready = {
            **READY,
            "rex_url": "https://rex-box-test.example.ts.net",
            "rex_hostname": "rex-box-test.example.ts.net",
            "rex_host_id": "saved-rex-host-id",
        }
        sb = self.sandbox(output=json.dumps(ready))
        with patch("box.environment.modal.Secret.from_name") as secret:
            self.assertEqual(start_network(sb, "box-test", secret_name="unused"), ready)
        secret.assert_not_called()
        sb.exec.assert_called_once()

    def test_rejects_invalid_readiness_address_or_identity(self):
        for result in [
            "not json",
            json.dumps({**READY, "tailscale_ip": "127.0.0.1"}),
            json.dumps({**READY, "hostname": "other"}),
        ]:
            with (
                self.subTest(result=result),
                self.assertRaisesRegex(RuntimeError, "Invalid Tailscale"),
            ):
                start_network(self.sandbox(output=result), "box-test")

    def test_redacts_auth_key_from_exec_failure(self):
        with self.assertRaisesRegex(RuntimeError, r"bad \[redacted\]"):
            start_network(
                self.sandbox(error="bad secret-value", exit_code=1),
                "box-test",
                auth_key="secret-value",
            )


class SSHCredentialTests(unittest.TestCase):
    def test_boot_does_not_source_a_saved_token_before_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            stale = Path(temporary) / "old.sh"
            stale.write_text("export OP_SERVICE_ACCOUNT_TOKEN=stale-token\n")
            result = subprocess.run(
                [*ENTRYPOINT[:-1], "-c", 'test "$OP_SERVICE_ACCOUNT_TOKEN" = new-token'],
                env={"BASH_ENV": str(stale), "OP_SERVICE_ACCOUNT_TOKEN": "new-token"},
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)

    def test_token_is_shell_quoted_and_root_only(self):
        token = "a'$(printf injected)\nsecond line; echo unsafe"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "box"
            with patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": token}, clear=True):
                ssh_env.write_environment(directory)
            path = directory / "ssh-env.sh"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    '. "$1"; test "$OP_SERVICE_ACCOUNT_TOKEN" = "$EXPECTED"',
                    "bash",
                    str(path),
                ],
                env={"EXPECTED": token},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_resume_replaces_saved_token_and_clears_it_when_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for token in ("old-token", "new-token"):
                with patch.dict(os.environ, {"OP_SERVICE_ACCOUNT_TOKEN": token}, clear=True):
                    ssh_env.write_environment(directory)
                self.assertEqual(
                    (directory / "ssh-env.sh").read_text(),
                    f"export OP_SERVICE_ACCOUNT_TOKEN={token}\nunset CLAUDE_CODE_OAUTH_TOKEN\n",
                )
            with patch.dict(os.environ, {}, clear=True):
                ssh_env.write_environment(directory)
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    '. "$1"; test -z "${OP_SERVICE_ACCOUNT_TOKEN+x}"',
                    "bash",
                    str(directory / "ssh-env.sh"),
                ],
                env={"OP_SERVICE_ACCOUNT_TOKEN": "stale-token"},
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(list(directory.iterdir()), [directory / "ssh-env.sh"])

    def test_tailscale_keys_are_never_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.dict(os.environ, {"TAILSCALE_AUTHKEY": "never-persist"}, clear=True):
                ssh_env.write_environment(directory)
            self.assertEqual(
                (directory / "ssh-env.sh").read_text(),
                "unset OP_SERVICE_ACCOUNT_TOKEN\nunset CLAUDE_CODE_OAUTH_TOKEN\n",
            )


if __name__ == "__main__":
    unittest.main()
