"""Exercise the actual CLI entry point, including JSON failures and process exits."""

import importlib
import json
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from box.config import Config, load_config

cli_module = importlib.import_module("box.cli")


def invoke_main(args):
    # CliRunner normally bypasses main(), where operational errors are formatted.
    runner = CliRunner()
    with runner.isolation() as streams, patch.object(sys, "argv", ["box", *args]):
        try:
            cli_module.main()
        except SystemExit as exc:
            code = exc.code
        else:
            code = 0
    return code, streams[0].getvalue().decode(), streams[1].getvalue().decode()


class CommandService:
    def __init__(self):
        self.calls = []
        self.error = None
        self.registry = self
        self.execute_result = {"exit_code": 0, "stdout": "", "stderr": ""}

    def get(self, name):
        return None

    def status(self, name):
        if self.error:
            raise self.error
        return {"name": name, "status": "running", "host": "100.64.0.2"}

    def execute(self, name, command, *, capture, cwd=None):
        self.calls.append((name, command, capture, cwd))
        if self.error:
            raise self.error
        return self.execute_result

    def up(self, config, **kwargs):
        self.calls.append((config, kwargs))
        return {"name": config.name, "status": "running"}


@pytest.fixture
def commands(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    manager = CommandService()
    monkeypatch.setattr(cli_module, "Service", lambda: manager)
    return manager


def test_noninteractive_default_is_one_json_document(commands):
    code, stdout, stderr = invoke_main(["status", "dev"])
    assert code == 0
    assert json.loads(stdout) == {"name": "dev", "status": "running", "host": "100.64.0.2"}
    assert len(stdout.splitlines()) == 1
    assert stderr == ""


@pytest.mark.parametrize("command", ["up", "resume"])
@pytest.mark.parametrize("options", [[], ["--json"], ["--human"]])
def test_build_progress_preserves_output_mode(commands, monkeypatch, command, options):
    def up(config, **kwargs):
        with cli_module.modal.enable_output():
            print("Building image...", flush=True)
        return {"name": config.name, "status": "running"}

    monkeypatch.setattr(commands, "up", up)
    code, stdout, stderr = invoke_main([*options, command, "dev"])
    assert code == 0
    if options == ["--human"]:
        assert stdout == "Building image...\ndev: running\n"
        assert stderr == ""
    else:
        assert json.loads(stdout) == {"name": "dev", "status": "running"}
        assert stderr == "Building image...\n"


def test_json_error_is_nonzero_and_redacts_auth_key(commands, monkeypatch):
    monkeypatch.setenv("TAILSCALE_AUTH_KEY", "secret-do-not-print")
    commands.error = RuntimeError("could not use secret-do-not-print")
    code, stdout, stderr = invoke_main(["--json", "status", "dev"])
    assert code == 1
    assert json.loads(stdout) == {
        "error": {"type": "RuntimeError", "message": "could not use [redacted]"}
    }
    assert "secret-do-not-print" not in stdout + stderr
    assert stderr == ""


def test_human_errors_go_to_stderr(commands):
    commands.error = ValueError("unknown box")
    code, stdout, stderr = invoke_main(["--human", "status", "dev"])
    assert code == 1
    assert stdout == ""
    assert "unknown box" in stderr


@pytest.mark.parametrize(
    "global_options,remote_flag,as_json",
    [
        ([], "--human", True),
        (["--json"], "--human", True),
        (["--human"], "--json", False),
    ],
)
def test_remote_arguments_cannot_change_error_output_mode(
    commands, global_options, remote_flag, as_json
):
    commands.error = ValueError("box is paused")
    code, stdout, stderr = invoke_main(
        [
            *global_options,
            "exec",
            "dev",
            "--",
            "printf",
            remote_flag,
        ]
    )
    assert code == 1
    assert commands.calls == [("dev", ("printf", remote_flag), as_json, None)]
    if as_json:
        assert json.loads(stdout)["error"]["message"] == "box is paused"
        assert stderr == ""
    else:
        assert stdout == ""
        assert "box is paused" in stderr


@pytest.mark.parametrize("exit_code", [0, 7, 127])
def test_exec_preserves_remote_exit_and_arguments(commands, exit_code):
    args = ("python", "-c", "print('hello')", "--flag", "a b", "", "$(touch /tmp/no)", ";")
    commands.execute_result = {"exit_code": exit_code, "stdout": "hello\n", "stderr": "warning\n"}
    code, stdout, stderr = invoke_main(["--json", "exec", "dev", "--", *args])
    assert code == exit_code
    assert json.loads(stdout) == commands.execute_result
    assert commands.calls == [("dev", args, True, None)]
    assert stderr == ""


def test_human_exec_requests_streaming_and_preserves_exit(commands):
    commands.execute_result = {"exit_code": 9}
    code, stdout, stderr = invoke_main(["--human", "exec", "dev", "--", "false"])
    assert code == 9
    assert commands.calls == [("dev", ("false",), False, None)]
    assert stdout == stderr == ""


@pytest.mark.parametrize("flag", ["--cwd", "-w"])
@pytest.mark.parametrize("path,normalized", [("/", "/"), ("/workspace//a b/", "/workspace/a b")])
def test_exec_cwd_is_a_command_only_override(commands, flag, path, normalized):
    code, stdout, stderr = invoke_main(["exec", flag, path, "dev", "--", "pwd"])
    assert code == 0
    assert json.loads(stdout)["exit_code"] == 0
    assert commands.calls == [("dev", ("pwd",), True, normalized)]
    assert stderr == ""


@pytest.mark.parametrize("path", ["", "relative", "//server/path", "/a/../b", "/a\x00b"])
def test_invalid_exec_cwd_is_a_usage_error_before_service_initialization(monkeypatch, path):
    monkeypatch.setattr(cli_module, "Service", lambda: pytest.fail("must stay offline"))
    code, stdout, _ = invoke_main(["exec", "--cwd", path, "dev", "--", "pwd"])
    assert code == 2
    assert "--cwd" in json.loads(stdout)["error"]["message"]


def test_remote_cwd_flags_after_separator_are_never_consumed(commands):
    args = ("some-command", "--cwd", "/remote-argument", "-w", "literal")
    code, _, _ = invoke_main(["exec", "-w", "/", "dev", "--", *args])
    assert code == 0
    assert commands.calls == [("dev", args, True, "/")]


def test_exec_help_advertises_cwd_without_provider_access(monkeypatch):
    monkeypatch.setattr(cli_module, "Service", lambda: pytest.fail("help must stay offline"))
    code, stdout, stderr = invoke_main(["exec", "--help"])
    assert code == 0
    assert "--cwd" in stdout and "-w" in stdout
    assert stderr == ""


def test_human_status_explains_incomplete_setup(commands, monkeypatch):
    monkeypatch.setattr(
        commands,
        "status",
        lambda name: {
            "name": name,
            "status": "setup-incomplete",
            "setup": {"complete": False, "phase": "clones"},
        },
    )
    code, stdout, stderr = invoke_main(["--human", "status", "dev"])
    assert code == 0
    assert "Setup incomplete at clones; run box up dev to finish." in stdout
    assert stderr == ""


def test_resume_uses_explicit_name_without_loading_unrelated_config(commands, tmp_path):
    (tmp_path / "box.toml").write_text("this is invalid toml")
    code, stdout, _ = invoke_main(["resume", "saved"])
    assert code == 0
    assert json.loads(stdout)["name"] == "saved"
    config, kwargs = commands.calls[0]
    assert config.name == "saved"
    assert kwargs == {"resume_only": True}


def test_cli_clone_and_volume_options_build_portable_config(commands, tmp_path):
    source = tmp_path / "repo with spaces"
    source.mkdir()
    code, _, _ = invoke_main(
        [
            "up",
            "dev",
            "--clone",
            str(source),
            "--volume",
            "cache:/mnt/cache",
            "--timeout",
            "600",
        ]
    )
    assert code == 0
    config, _ = commands.calls[0]
    assert config.clones == [{"source": str(source), "destination": "/workspace/repo with spaces"}]
    assert config.volumes == {"/mnt/cache": "cache"}
    assert config.timeout == 600


def test_noninteractive_remove_requires_explicit_yes(commands):
    code, stdout, _ = invoke_main(["rm", "dev"])
    assert code == 1
    assert "--yes" in json.loads(stdout)["error"]["message"]


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Bad Name"},
        {"name": "a" * 41},
        {"name": "-option"},
        {"workdir": "/"},
        {"workdir": "/."},
        {"workdir": "/./"},
        {"workdir": "relative"},
        {"workdir": "/workspace/../etc"},
        {"cpu": 0},
        {"cpu": True},
        {"cpu": float("nan")},
        {"cpu": float("inf")},
        {"memory": 1.5},
        {"memory": False},
        {"timeout": 86401},
        {"timeout": 0},
        {"idle_timeout": True},
        {"idle_timeout": 1.5},
        {"tailscale_secret": ""},
        {"region": ""},
        {"secrets": "secret"},
        {"secrets": [""]},
        {"volumes": {"/etc": "data"}},
        {"volumes": {"/mnt/a": "x", "/mnt/a/b": "y"}},
        {"volumes": {"/workspace/": "x", "/workspace": "y"}},
        {"clones": [{"source": "/tmp/a", "destination": "/workspace", "unknown": True}]},
        {"clones": [{"source": "/tmp/a", "exclude": "*.env"}]},
        {
            "clones": [
                {"source": "/tmp/a", "destination": "/workspace/a"},
                {"source": "/tmp/b", "destination": "/workspace/a/b"},
            ]
        },
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        Config(**changes).validate()


def test_configuration_resolves_sources_relative_to_file_and_defaults_destination(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = project / "box.toml"
    cfg.write_text(
        'name = "custom"\nworkdir = "/workspace/project"\n'
        '[[clones]]\nsource = "../source"\nexclude = ["*.tmp"]\n'
    )
    value = load_config(cfg)
    assert value.name == "custom"
    assert value.clones == [
        {
            "source": str(tmp_path / "source"),
            "destination": "/workspace/project",
            "exclude": ["*.tmp"],
        }
    ]
    assert Config(**value.to_dict()).validate() == value


def test_validated_config_normalizes_paths_and_clone_destination():
    cfg = Config(
        workdir="/workspace//project/",
        volumes={"/mnt//cache/": "cache"},
        clones=[{"source": "/tmp/project"}],
    ).validate()
    assert cfg.workdir == "/workspace/project"
    assert cfg.volumes == {"/mnt/cache": "cache"}
    assert cfg.clones[0]["destination"] == "/workspace/project"


def test_unknown_config_key_and_missing_explicit_file_are_rejected(tmp_path):
    path = tmp_path / "box.toml"
    with pytest.raises(ValueError, match="does not exist"):
        load_config(path)
    path.write_text('typo = "value"\n')
    with pytest.raises(ValueError, match="Unknown configuration keys: typo"):
        load_config(path)


def test_help_does_not_initialize_modal(monkeypatch):
    def forbidden():
        pytest.fail("help must not initialize provider state")

    monkeypatch.setattr(cli_module, "Service", forbidden)
    code, stdout, stderr = invoke_main(["--help"])
    assert code == 0
    assert "Portable development environments" in stdout
    assert stderr == ""


@pytest.mark.parametrize("failure", [None, "auth", "connection"])
def test_doctor_checks_auth_and_distinguishes_connectivity(monkeypatch, tmp_path, failure):
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)

    def connect():
        if failure == "auth":
            raise cli_module.modal.exception.AuthError("invalid token")
        if failure == "connection":
            raise TimeoutError("private provider diagnostic")

    monkeypatch.setattr(cli_module, "connect", connect)
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout='{"BackendState": "Running"}'),
    )
    code, stdout, stderr = invoke_main(["--json", "doctor"])
    value = json.loads(stdout)
    assert code == (1 if failure else 0)
    if failure == "auth":
        assert "Authentication failed" in value["modal"]
    elif failure == "connection":
        assert "Connection check failed" in value["modal"]
        assert "private provider diagnostic" not in stdout
    else:
        assert value["modal"] == "authenticated"
    assert stderr == ""
