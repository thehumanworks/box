"""CLI contracts for HTTPS exec, background submission and repeatable polling."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from test_cli import cli_module, invoke_main
from test_cli import commands as commands


def terminal(code=0):
    return {
        "call_id": "fc-test",
        "status": "completed",
        "result": {
            "status": "completed",
            "exit_code": code,
            "stdout": "out",
            "stderr": "err",
        },
    }


@pytest.mark.parametrize("command", ["exec", "spawn", "poll"])
def test_remote_commands_never_initialize_modal_service(commands, monkeypatch, command):
    forbidden = Mock(side_effect=AssertionError("Must not connect directly to Modal"))
    monkeypatch.setattr(cli_module, "Service", forbidden)
    monkeypatch.setattr(cli_module, "connect", forbidden)
    args = ["poll", "fc-test"] if command == "poll" else [command, "dev", "--", "true"]
    code, _, _ = invoke_main(args)
    assert code == (75 if command == "poll" else 0)
    forbidden.assert_not_called()


@pytest.mark.parametrize("mode", ["--json", "--human"])
def test_spawn_returns_handle_without_waiting(commands, mode):
    code, stdout, stderr = invoke_main(
        [
            mode,
            "spawn",
            "-w",
            "/",
            "--timeout",
            "120",
            "--request-id",
            "request-123",
            "dev",
            "--",
            "sleep",
            "10",
        ]
    )
    assert code == 0
    if mode == "--human":
        assert stdout == "fc-test\n"
    else:
        import json

        assert json.loads(stdout) == commands.spawn_result
    assert stderr == ""
    assert commands.calls == [("spawn", "dev", ("sleep", "10"), "/", 120, "request-123")]


@pytest.mark.parametrize("mode", ["--json", "--human"])
def test_poll_preserves_terminal_command_exit_and_streams(commands, mode):
    import json

    commands.poll_results = [terminal(23)]
    code, stdout, stderr = invoke_main([mode, "poll", "fc-test"])
    assert code == 23
    if mode == "--human":
        assert stdout == "out" and stderr == "err"
    else:
        assert json.loads(stdout) == terminal(23) and stderr == ""


def test_pending_poll_has_a_distinct_exit_status(commands):
    import json

    code, stdout, stderr = invoke_main(["poll", "fc-test"])
    assert code == 75 and json.loads(stdout)["status"] == "pending"
    assert stderr == ""


def test_wait_only_repeats_reads_and_emits_one_document(commands, monkeypatch):
    import json

    commands.poll_results.append(terminal(7))
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: None)
    code, stdout, stderr = invoke_main(["poll", "--wait", "fc-test"])
    assert code == 7 and json.loads(stdout) == terminal(7)
    assert stderr == "" and len(stdout.splitlines()) == 1
    assert len(commands.calls) == 2 and all(call[0] == "poll" for call in commands.calls)


def test_wait_deadline_leaves_job_pending_without_more_requests(commands, monkeypatch):
    clock = [0]
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        cli_module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    code, stdout, _ = invoke_main(["poll", "--wait", "--timeout", "0.1", "fc-test"])
    assert code == 75 and '"pending"' in stdout
    assert len(commands.calls) == 1


def test_interrupted_wait_preserves_call_id_and_does_not_cancel(commands, monkeypatch):
    import json

    monkeypatch.setattr(cli_module.time, "sleep", Mock(side_effect=KeyboardInterrupt()))
    code, stdout, _ = invoke_main(["poll", "--wait", "fc-test"])
    value = json.loads(stdout)
    assert code == 130 and value["call_id"] == "fc-test"
    assert "not cancelled" in value["error"]["message"]
    assert len(commands.calls) == 1


@pytest.mark.parametrize(
    "value,exit_code",
    [
        ({"status": "expired", "call_id": "fc-test"}, 1),
        ({"status": "failed", "error": {"code": "unknown_call", "message": "Unknown"}}, 1),
        (
            {
                "status": "failed",
                "result": {
                    "status": "failed",
                    "exit_code": -1,
                    "error": {"code": "exec_timeout", "message": "Timed out"},
                },
            },
            124,
        ),
    ],
)
def test_expired_failed_and_timed_out_calls_are_not_success(commands, value, exit_code):
    import json

    commands.poll_results = [value]
    code, stdout, _ = invoke_main(["poll", "fc-test"])
    assert code == exit_code and json.loads(stdout) == value


def test_human_streams_preserve_ansi_when_redirected(commands):
    commands.execute_result.update(stdout="\x1b[31mout\x1b[0m", stderr="\x1b[32merr\x1b[0m")
    code, stdout, stderr = invoke_main(["--human", "exec", "dev", "--", "true"])
    assert code == 0
    assert stdout == commands.execute_result["stdout"]
    assert stderr == commands.execute_result["stderr"]


def test_human_truncation_is_visible_without_polluting_stdout(commands):
    commands.execute_result.update(stdout="out", stdout_truncated=True)
    code, stdout, stderr = invoke_main(["--human", "exec", "dev", "--", "true"])
    assert code == 0 and stdout == "out"
    assert "truncated" in stderr


@pytest.mark.parametrize("use_flag", [True, False])
def test_manifest_can_be_selected_without_loading_project_config(
    commands, monkeypatch, tmp_path, use_flag
):
    (tmp_path / "box.toml").write_text("invalid toml")
    path = tmp_path / "selected.json"
    factory = Mock(return_value=commands)
    monkeypatch.setattr(cli_module, "RemoteClient", factory)
    monkeypatch.setenv(
        "BOX_REMOTE_DEPLOYMENT", str(path if not use_flag else tmp_path / "ignored.json")
    )
    options = ["--remote-deployment", str(path)] if use_flag else []
    code, _, _ = invoke_main([*options, "exec", "dev", "--", "true"])
    assert code == 0
    factory.assert_called_once_with(Path(path))


@pytest.mark.parametrize(
    "args",
    [
        ["exec", "--timeout", "91", "dev", "--", "true"],
        ["spawn", "--timeout", "3601", "dev", "--", "true"],
        ["spawn", "--request-id", "bad", "dev", "--", "true"],
        ["spawn", "--cwd", "relative", "dev", "--", "true"],
        ["poll", "bad-id"],
        ["poll", "--timeout", "0", "fc-test"],
        ["poll", "--interval", "0", "fc-test"],
        ["poll", "--timeout", "nan", "fc-test"],
        ["poll", "--interval", "nan", "fc-test"],
    ],
)
def test_invalid_rpc_options_are_usage_errors_before_client_access(monkeypatch, args):
    monkeypatch.setattr(cli_module, "RemoteClient", Mock(side_effect=AssertionError("offline")))
    code, _, _ = invoke_main(args)
    assert code == 2


@pytest.mark.parametrize("command", ["exec", "spawn", "poll"])
def test_rpc_help_stays_offline(monkeypatch, command):
    monkeypatch.setattr(cli_module, "RemoteClient", Mock(side_effect=AssertionError("offline")))
    code, stdout, stderr = invoke_main([command, "--help"])
    assert code == 0 and "--timeout" in stdout and not stderr


def test_remote_timeout_and_id_flags_after_separator_remain_literal(commands):
    args = ("echo", "--timeout", "8000", "--request-id", "not-an-id")
    code, _, _ = invoke_main(["exec", "--timeout", "10", "dev", "--", *args])
    assert code == 0
    assert commands.calls == [("dev", args, None, 10, None)]
