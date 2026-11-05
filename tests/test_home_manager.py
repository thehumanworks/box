"""Contracts between Home Manager, the runtime bootstrap and remote commands."""

import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from box.environment import ASSETS, ENTRYPOINT
from box.service import Service


def test_locked_home_configuration_has_one_nixpkgs_and_no_runtime_secrets():
    lock = json.loads((ASSETS / "home/flake.lock").read_text())
    assert lock["nodes"]["home-manager"]["inputs"]["nixpkgs"] == ["nixpkgs"]
    for name in ("nixpkgs", "home-manager"):
        locked = lock["nodes"][name]["locked"]
        assert len(locked["rev"]) == 40
        assert locked["narHash"].startswith("sha256-")
    for path in (ASSETS / "home").glob("*.nix"):
        source = path.read_text()
        assert "OP_SERVICE_ACCOUNT_TOKEN" not in source
        assert "TAILSCALE_AUTH" not in source
        assert "builtins.getEnv" not in source
        assert "builtins.readFile" not in source


@pytest.mark.parametrize("directory_state", ["existing", "missing", "file"])
def test_remote_exec_loads_environment_and_preserves_literal_arguments(
    tmp_path, monkeypatch, directory_state
):
    bridge = tmp_path / "profile.sh"
    bridge.write_text("export BOX_ENV_TEST=from-home-manager\n")
    marker = tmp_path / "unexpected"
    workdir = tmp_path / "work space; $(false) 'quoted'"
    if directory_state == "existing":
        workdir.mkdir()
        (workdir / "keep.txt").write_text("keep existing files")
    elif directory_state == "file":
        workdir.write_text("do not replace")
    literal = [f"$(touch {marker})", ";", "", "a b", "single'quote", 'double"quote']
    command = [
        sys.executable,
        "-c",
        "import json,os,sys; "
        "print(json.dumps([os.environ['BOX_ENV_TEST'], sys.argv[1:], os.getcwd()]))",
        *literal,
    ]

    class LocalSandbox:
        def exec(self, *args, **kwargs):
            # Use the actual shell program, replacing only its container-only path.
            args = [*args]
            args[2] = args[2].replace("/etc/profile.d/box.sh", shlex.quote(str(bridge)))
            result = subprocess.run(args, cwd=kwargs["workdir"], capture_output=True, text=True)
            return SimpleNamespace(
                stdout=io.StringIO(result.stdout),
                stderr=io.StringIO(result.stderr),
                wait=lambda: result.returncode,
            )

    service = Service(registry=object())
    monkeypatch.setattr(
        service,
        "running",
        lambda name, **kwargs: ({"config": {"workdir": str(workdir)}}, LocalSandbox()),
    )
    if directory_state == "existing":
        result = service.execute("dev", command, capture=True)
        assert result["exit_code"] == 0, result
        assert json.loads(result["stdout"]) == ["from-home-manager", literal, str(workdir)]
        assert (workdir / "keep.txt").read_text() == "keep existing files"
    else:
        # Local subprocess chdir fails before launching, just as Modal must.
        with pytest.raises((FileNotFoundError, NotADirectoryError)):
            service.execute("dev", command, capture=True)
        if directory_state == "file":
            assert workdir.read_text() == "do not replace"
        else:
            assert not workdir.exists()
    assert not marker.exists()


def test_shell_bridge_reloads_current_generation_and_session_path(tmp_path):
    home_profile = tmp_path / "hm-session-vars.sh"
    home_profile.write_text(
        'if [ -n "${__HM_SESS_VARS_SOURCED-}" ]; then return; fi\n'
        "export __HM_SESS_VARS_SOURCED=1\n"
        'export PATH="/custom-home-bin:$PATH"\nexport BOX_GENERATION=new\n'
        'export PATH="/nix-profile/bin:$PATH"\n'
    )
    script = (
        (ASSETS / "shell.sh")
        .read_text()
        .replace("/root/.nix-profile/etc/profile.d/hm-session-vars.sh", str(home_profile))
        .replace("/run/box/ssh-env.sh", str(tmp_path / "absent-secrets"))
    )
    bridge = tmp_path / "bridge.sh"
    bridge.write_text(script)
    env = {**os.environ, "__HM_SESS_VARS_SOURCED": "1", "BOX_GENERATION": "old"}
    process = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; first=$PATH; . "$1"; test "$BOX_GENERATION" = new; '
            'for candidate in "$first" "$PATH"; do '
            'case "$candidate" in /root/.local/share/mise/shims:/root/.local/bin:*) '
            ";; *) exit 2;; esac; "
            'case ":$candidate:" in *:/custom-home-bin:*) ;; *) exit 3;; esac; done',
            "bash",
            str(bridge),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def test_shell_bridge_handles_optional_hm_variables_without_losing_nounset(tmp_path):
    profile = tmp_path / "hm.sh"
    profile.write_text('export BOX_OPTIONAL="$BOX_UNSET_VARIABLE:/configured"\n')
    bridge = tmp_path / "bridge.sh"
    bridge.write_text(
        (ASSETS / "shell.sh")
        .read_text()
        .replace("/root/.nix-profile/etc/profile.d/hm-session-vars.sh", str(profile))
        .replace("/run/box/ssh-env.sh", str(tmp_path / "absent-secrets"))
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-uc",
            '. "$1"; test "$BOX_OPTIONAL" = :/configured; case $- in *u*) ;; *) exit 2;; esac',
            "bash",
            str(bridge),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("exit_trigger", ["tailscaled", "rex", "signal"])
def test_boot_ignores_profiles_strips_secrets_and_cleans_both_daemons(tmp_path, exit_trigger):
    profile = tmp_path / "broken-profile.sh"
    marker = tmp_path / "profile-was-sourced"
    profile.write_text(f"touch {shlex.quote(str(marker))}; exit 90\n")
    secrets = ("OP_SERVICE_ACCOUNT_TOKEN", "TAILSCALE_AUTHKEY", "TAILSCALE_AUTH_KEY", "TS_AUTHKEY")
    for name in ("tailscaled", "rex"):
        daemon = tmp_path / name
        program = tmp_path / f"{name}.py"
        program.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys, time\nfrom pathlib import Path\n"
            "p = Path(sys.argv[0]).with_suffix('')\n"
            "def stop(signum, frame):\n"
            "    p.with_suffix('.stopped').touch()\n    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"secrets = {secrets!r}\n"
            "p.with_suffix('.json').write_text(json.dumps({'pid': os.getpid(), "
            "'secrets': [k for k in secrets if k in os.environ], "
            "'profile': os.environ.get('BASH_ENV'), "
            "'runtime': os.environ.get('REX_RUNTIME_DIR')}))\n"
            f"if p.name == {exit_trigger!r}:\n"
            "    deadline = time.monotonic() + 5\n"
            "    while len(list(p.parent.glob('*.json'))) < 2:\n"
            "        if time.monotonic() > deadline: sys.exit(91)\n"
            "        time.sleep(0.01)\n"
            "    sys.exit(17)\n"
            "while True: time.sleep(0.1)\n"
        )
        # Nix packages may launch through Bash wrappers. An inherited BASH_ENV
        # would source the profile before the real daemon can start.
        daemon.write_text("#!/bin/bash\nexec " + shlex.join([sys.executable, str(program)]) + "\n")
        daemon.chmod(0o755)
    runtime = tmp_path / "boot_runtime.py"
    runtime.write_text(
        (ASSETS / "boot.py").read_text().replace("/etc/profile.d/box.sh", str(profile))
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\nfrom pathlib import Path\nimport boot_runtime as boot\n"
        f"sys.path.insert(0, {str(ASSETS)!r})\n"
        "import ssh_env\n"
        f"boot.RUNTIME = Path({str(tmp_path / 'run')!r})\n"
        f"boot.TAILSCALE_STATE = Path({str(tmp_path / 'tailscale-state')!r})\n"
        f"boot.REX_STATE = Path({str(tmp_path / 'rex-state')!r})\n"
        f"boot.PROFILE = {str(tmp_path) + '/'!r}\n"
        "original_write = ssh_env.write_environment\n"
        "ssh_env.write_environment = lambda: original_write(boot.RUNTIME)\n"
        "sys.exit(boot.main())\n"
    )
    boot = tmp_path / "boot.sh"
    boot.write_text(
        (ASSETS / "boot.sh")
        .read_text()
        .replace(
            "/usr/local/bin/python /opt/box/boot.py", shlex.join([sys.executable, str(runner)])
        )
    )
    process = subprocess.Popen(
        [*ENTRYPOINT[:-1], str(boot)],
        env={**os.environ, "BASH_ENV": str(profile), **dict.fromkeys(secrets, "fake-token")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if exit_trigger == "signal":
            deadline = time.monotonic() + 5
            while len(list(tmp_path.glob("*.json"))) < 2:
                assert time.monotonic() < deadline, "mock daemons did not start"
                time.sleep(0.01)
            process.terminate()
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        leaked = []
        for record in tmp_path.glob("*.json"):
            pid = json.loads(record.read_text())["pid"]
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            os.kill(pid, signal.SIGKILL)
            leaked.append(record.stem)
        assert not leaked, f"boot left daemons running: {leaked}"
    assert process.returncode == (0 if exit_trigger == "signal" else 17), (stdout, stderr)
    for name in ("tailscaled", "rex"):
        observed = json.loads((tmp_path / f"{name}.json").read_text())
        assert observed["secrets"] == []
        assert observed["profile"] is None
        assert observed["runtime"] == str(tmp_path / "run/rex")
        if name != exit_trigger:
            assert (tmp_path / f"{name}.stopped").exists()
    assert not marker.exists()


def test_writable_git_identity_does_not_replace_managed_defaults(tmp_path):
    managed = tmp_path / "store-git-config"
    managed.write_text("[init]\n\tdefaultBranch = main\n")
    xdg = tmp_path / ".config/git"
    xdg.mkdir(parents=True)
    (xdg / "config").symlink_to(managed)
    subprocess.run(
        ["git", "config", "--file", str(tmp_path / ".gitconfig"), "user.name", "Test Author"],
        check=True,
    )
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / ".config")}
    for key, expected in (("user.name", "Test Author"), ("init.defaultBranch", "main")):
        p = subprocess.run(
            ["git", "config", "--get", key],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert p.stdout.strip() == expected
    assert (xdg / "config").is_symlink()
