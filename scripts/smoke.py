"""Opt-in live integration check. Creates only uniquely named test resources."""

import json
import os
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import modal

from box.config import Config
from box.service import Service
from box.transfer import copy_from, copy_to


def verify_rex(record, evidence, *, phase):
    """Connect from this machine and execute a terminal on the test-owned server."""
    url = urlsplit(record["rex_url"])
    assert url.scheme == "https" and url.hostname == record["rex_hostname"], record
    assert url.username is None and url.password is None and not url.query, record
    assert record["rex_host_id"], record
    env = {key: value for key, value in os.environ.items() if not key.startswith("REX_")}

    def rex(*args):
        result = subprocess.run(
            ["rex", "-S", record["rex_url"], "--autostart=false", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (args, result.stdout[-4000:], result.stderr[-4000:])
        return result.stdout

    identity = json.loads(rex("whoami", "--json"))
    assert identity["host_id"] == record["rex_host_id"], identity
    label = "box-rex-smoke-" + uuid.uuid4().hex[:8]
    created = json.loads(rex("new", label, "--keep-open", "--json", "--", "true"))
    session = created["session_id"]
    marker = "rex-terminal-proof-" + uuid.uuid4().hex
    try:
        terminal = json.loads(
            rex(
                "run",
                "-s",
                session,
                "-c",
                "/workspace",
                "--label",
                label,
                "--keep-open",
                "--wait",
                "--json",
                "--",
                "bash",
                "-c",
                'set -eu; test "$TERM" = xterm-rex; command -v nix mise op rex; '
                'test -n "$OP_SERVICE_ACCOUNT_TOKEN"; op whoami >/dev/null; '
                "printf '%s\\n' " + shlex.quote(marker),
            )
        )
        output = rex("capture", "-s", session, "-b", terminal["block_ids"][0], "--trim")
        assert marker in output, output[-4000:]
        evidence.setdefault("rex", {})[phase] = {
            "url": record["rex_url"],
            "host_id": identity["host_id"],
            "version": identity["rex_version"],
            "terminal_output": output[-4000:],
        }
        evidence["checks"].append(f"Rex HTTPS identity, terminal, shell and 1Password ({phase})")
    finally:
        rex("kill", session)


def cleanup_test_box(service, name):
    """Log out only this smoke box's two network identities before removing it."""
    if not service.registry.get(name):
        return
    try:
        record = service.require(name)
        if record.get("sandbox_id"):
            sb = service.sandbox(record)
            if sb.poll() is None:
                results = []
                for command in (
                    (
                        "/root/.nix-profile/bin/rex",
                        "-S",
                        "unix:///run/box/rex/server.sock",
                        "--autostart=false",
                        "server",
                        "tailscale",
                        "logout",
                    ),
                    ("tailscale", "--socket=/run/box/tailscaled.sock", "logout"),
                ):
                    process = sb.exec(*command, timeout=30)
                    results.append((command[0], process.wait()))
                assert all(code == 0 for _, code in results), results
    finally:
        service.remove(name)


def verify_rex_environment(service, name, evidence):
    info = service.execute(name, ["rex", "terminfo", "status", "--json"], capture=True)
    assert info["exit_code"] == 0, info
    terminal = json.loads(info["stdout"])
    assert terminal["guard"] == "active" and terminal["downgrade_in_effect"], terminal
    check = service.execute(
        name,
        [
            "python",
            "-c",
            """import json, subprocess
from pathlib import Path
server = json.loads(subprocess.check_output(
    ['rex', '--autostart=false', 'server', 'status', '--json']))
pids = [server['pid']]
for entry in Path('/proc').iterdir():
    if entry.name.isdigit():
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\\0')
        except (FileNotFoundError, PermissionError):
            continue
        if b'--socket=/run/box/tailscaled.sock' in argv:
            pids.append(int(entry.name))
assert len(pids) == 2, 'expected both managed daemons'
forbidden = {b'OP_SERVICE_ACCOUNT_TOKEN', b'CLAUDE_CODE_OAUTH_TOKEN', b'TAILSCALE_AUTHKEY',
             b'TAILSCALE_AUTH_KEY', b'TS_AUTHKEY'}
for pid in pids:
    entries = Path('/proc', str(pid), 'environ').read_bytes().split(b'\\0')
    keys = {x.split(b'=', 1)[0] for x in entries}
    assert not forbidden.intersection(keys), 'daemon retained a credential'
print('Both managed daemon environments exclude enrollment and service tokens')
""",
        ],
        capture=True,
    )
    assert check["exit_code"] == 0, check
    evidence["checks"].append("Rex terminfo/SSH guard and credential-free daemon environments")


def verify_shell_paths(service, name, ssh_args, evidence, *, changed=False):
    code = (
        "import json,os,shutil; "
        "print(json.dumps({'path':os.environ['PATH'], "
        "'managed':os.environ.get('BOX_HM_SMOKE'), 'hello':shutil.which('hello')}))"
    )
    command = "python3 -c " + shlex.quote(code)
    direct = service.execute(name, ["python3", "-c", code], capture=True)
    assert direct["exit_code"] == 0, direct
    outputs = {"box exec": direct["stdout"]}
    for mode, remote in (
        ("SSH command", command),
        ("SSH interactive", "bash -ic " + shlex.quote(command)),
        ("SSH login", "bash -lic " + shlex.quote(command)),
    ):
        p = subprocess.run(
            [*ssh_args[:-1], "-tt", ssh_args[-1], remote],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert p.returncode == 0, (mode, p.stdout, p.stderr)
        outputs[mode] = p.stdout
    states = {mode: json.loads(output) for mode, output in outputs.items()}
    for mode, state in states.items():
        assert state["path"].startswith("/root/.local/share/mise/shims:/root/.local/bin:"), (
            mode,
            state,
        )
        assert state["managed"] == ("managed" if changed else None), (mode, state)
        if changed:
            assert state["hello"] == "/root/.local/share/mise/shims/hello", (mode, state)
    evidence.setdefault("shell_paths", {})["changed" if changed else "initial"] = states


def verify_home_manager(service, name, ssh_args, evidence):
    """Exercise real activation and rollback; leave an unapplied edit for resume."""

    def run(*command, success=True):
        response = service.execute(name, command, capture=True, timeout=300)
        assert (response["exit_code"] == 0) == success, response
        return response["stdout"].strip()

    generation_command = (
        "python",
        "-c",
        "from pathlib import Path; "
        "paths=[Path('/root/.local/state/nix/profiles/home-manager'), "
        "Path('/nix/var/nix/profiles/per-user/root/home-manager')]; "
        "print(next(str(p.resolve()) for p in paths if p.exists()))",
    )
    original_generation = run(*generation_command)
    identity = run("git", "config", "--get", "user.name")
    run("home-manager", "build", "--no-out-link", "--no-update-lock-file")
    run("home-manager", "switch", "--no-update-lock-file")
    run("home-manager", "switch", "--no-update-lock-file")
    assert run(*generation_command) == original_generation
    run(
        "bash",
        "-c",
        "set -eu; test -L ~/.bashrc; test -L ~/.config/git/config; "
        "test ! -L ~/.config/home-manager/home.nix; test ! -L ~/.gitconfig; "
        'nix --version | grep -F "Determinate Nix"',
    )
    evidence["checks"].append("Home Manager build and repeat activation")

    run(
        "python",
        "-c",
        """from pathlib import Path
p = Path('/root/.config/home-manager/home.nix')
s = p.read_text().replace('home.packages = with pkgs; [', 'home.packages = with pkgs; [ hello')
s = s.replace('EDITOR = "nvim";', 'EDITOR = "nvim"; BOX_HM_SMOKE = "managed";')
p.write_text(s)
""",
    )
    run("home-manager", "switch", "--no-update-lock-file")
    assert "Hello, world!" in run("hello")
    assert run("printenv", "BOX_HM_SMOKE") == "managed"
    assert run("git", "config", "--get", "user.name") == identity
    run(
        "bash",
        "-c",
        "set -eu; mkdir -p ~/.local/share/mise/shims; "
        "printf '#!/bin/sh\\necho mise-priority\\n' > ~/.local/share/mise/shims/hello; "
        "chmod +x ~/.local/share/mise/shims/hello",
    )
    verify_shell_paths(service, name, ssh_args, evidence, changed=True)
    run("rm", "/root/.local/share/mise/shims/hello")
    evidence["checks"].append("mise precedence and managed variables agree across exec/SSH modes")
    changed_generation = run(*generation_command)
    assert changed_generation != original_generation
    run("home-manager", "switch", "--rollback")
    assert run(*generation_command) == original_generation
    run(
        "bash",
        "-c",
        'set -eu; test -z "$(command -v hello || true)"; test -z "${BOX_HM_SMOKE-}"',
    )
    assert run("git", "config", "--get", "user.name") == identity
    run(
        "bash",
        "-c",
        "set -eu; test -L ~/.config/git/config; "
        "test -f ~/.config/mise/config.toml; test ! -L ~/.config/mise/config.toml",
    )
    evidence["checks"].append(
        "Home Manager package/config changes and rollback preserve Git identity"
    )

    run(
        "python",
        "-c",
        """from pathlib import Path
p = Path('/root/.config/home-manager/home.nix')
p.with_suffix('.saved').write_text(p.read_text())
p.write_text(p.read_text() + '\\nthis is invalid nix {{{')
""",
    )
    run("home-manager", "switch", success=False)
    assert run(*generation_command) == original_generation
    run(
        "python",
        "-c",
        """from pathlib import Path
p = Path('/root/.config/home-manager/home.nix')
p.write_text(p.with_suffix('.saved').read_text())
p.with_suffix('.saved').unlink()
""",
    )
    evidence["checks"].append("failed Home Manager build preserves active generation")
    run(
        "python",
        "-c",
        """import os
from pathlib import Path
token = os.environ['OP_SERVICE_ACCOUNT_TOKEN'].encode()
assert token
for p in [Path('/root/.bashrc'), Path('/root/.profile'), Path('/root/.bash_profile'),
          Path('/root/.config/git/config'),
          Path('/root/.nix-profile/etc/profile.d/hm-session-vars.sh'),
          *Path('/root/.config/home-manager').glob('*')]:
    if p.is_file():
        assert token not in p.read_bytes(), 'runtime token found in generated configuration'
""",
    )
    evidence["checks"].append("runtime credential excluded from Home Manager configuration")
    evidence["home_manager_generation"] = original_generation
    return generation_command


def main():
    name = "box-smoke-" + uuid.uuid4().hex[:8]
    volume = name + "-data"
    service = Service()
    evidence = {"name": name, "volume": volume, "checks": []}
    local_rex = subprocess.run(
        ["rex", "version"], capture_output=True, text=True, timeout=15, check=True
    )
    evidence["local_rex_version"] = local_rex.stdout.strip()
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="box-live-") as tmp:
            source = Path(tmp) / "project"
            source.mkdir()
            (source / "empty").mkdir()
            (source / ".hidden").write_text("original\n")
            (source / "run.sh").write_text("#!/bin/sh\necho clone-ok\n")
            (source / "run.sh").chmod(0o755)
            (source / "link").symlink_to(".hidden")
            config = Config(
                name=name,
                timeout=1800,
                clones=[{"source": str(source), "destination": "/workspace"}],
                volumes={"/mnt/data": volume},
            )
            record = service.up(config)
            evidence["initial"] = record
            print(json.dumps({"created": record}), flush=True)
            verify_rex_environment(service, name, evidence)
            verify_rex(record, evidence, phase="initial")
            checks = service.execute(
                name,
                [
                    "bash",
                    "-lc",
                    "set -eu; nix --version; mise --version; op --version; rex version; "
                    'test -n "$OP_SERVICE_ACCOUNT_TOKEN"; '
                    "git --version; python --version; node --version; uv --version; "
                    'test -d empty; test -x run.sh; test "$(cat link)" = original; '
                    "echo remote > .hidden; echo volume > /mnt/data/marker",
                ],
                capture=True,
            )
            assert checks["exit_code"] == 0, checks
            evidence["tools"] = checks["stdout"]
            evidence["checks"].append("tools and initial clone")
            nix = service.execute(
                name,
                [
                    "nix",
                    "build",
                    "--offline",
                    "--impure",
                    "--no-link",
                    "--expr",
                    'derivation { name = "box-smoke"; system = builtins.currentSystem; '
                    'builder = "/bin/sh"; args = [ "-c" "echo ok > $out" ]; }',
                ],
                capture=True,
            )
            assert nix["exit_code"] == 0, nix
            evidence["checks"].append("actual Nix derivation build and 1Password secret injection")
            record0, sb = service.running(name)
            copy_to(sb, source, "/mnt/data/later", mounts=tuple(config.volumes))
            copy_to(sb, source, "/workspace/later")
            evidence["checks"].append("later clone and volume transfer")
            for shell in [
                "set -eu; nix --version; mise --version; op --version; "
                'test -n "$OP_SERVICE_ACCOUNT_TOKEN"; op whoami >/dev/null',
                "bash -ic 'command -v nix mise git python node uv'",
                "bash -lic 'command -v nix mise git python node uv'",
            ]:
                ssh = subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "StrictHostKeyChecking=accept-new",
                        "-o",
                        "ConnectTimeout=20",
                        "-o",
                        f"UserKnownHostsFile={tmp}/known_hosts",
                        "root@" + record["host"],
                        shell,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert ssh.returncode == 0, (ssh.returncode, ssh.stdout, ssh.stderr)
            evidence["checks"].append(
                "actual Tailscale SSH: noninteractive, interactive and login shell"
            )
            ssh_args = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=20",
                "-o",
                f"UserKnownHostsFile={tmp}/known_hosts",
                "root@" + record["host"],
            ]
            verify_shell_paths(service, name, ssh_args, evidence)
            generation_command = verify_home_manager(service, name, ssh_args, evidence)
            paused = service.pause(name)
            evidence["snapshot_id"] = paused["snapshot_id"]
            (source / ".hidden").write_text("local changed\n")
            resumed = service.up(Config(name=name), resume_only=True)
            assert resumed["sandbox_id"] != record["sandbox_id"]
            assert resumed["host"] == record["host"], (record, resumed)
            for key in ("rex_url", "rex_hostname", "rex_host_id"):
                assert resumed[key] == record[key], (key, record, resumed)
            verify_rex(resumed, evidence, phase="snapshot resume")
            checks = service.execute(
                name,
                [
                    "bash",
                    "-lc",
                    'set -eu; test "$(cat .hidden)" = remote; '
                    'test "$(cat /mnt/data/marker)" = volume; test -f later/.hidden; '
                    "nix --version; mise --version; op --version; "
                    'test -n "$OP_SERVICE_ACCOUNT_TOKEN"',
                ],
                capture=True,
            )
            assert checks["exit_code"] == 0, checks
            evidence["checks"].append(
                "snapshot resume preserves remote edits, volume and Tailscale identity"
            )
            hm = service.execute(name, generation_command, capture=True)
            assert hm["stdout"].strip() == evidence["home_manager_generation"], hm
            hm = service.execute(
                name,
                [
                    "bash",
                    "-c",
                    'set -eu; grep -F "[ hello" ~/.config/home-manager/home.nix; '
                    'test -z "$(command -v hello || true)"; '
                    "home-manager --version; op whoami >/dev/null",
                ],
                capture=True,
            )
            assert hm["exit_code"] == 0, hm
            evidence["checks"].append(
                "snapshot retains Home Manager generation and unapplied source edit"
            )
            _, sb = service.running(name)
            copy_from(sb, "/workspace", Path(tmp) / "pulled")
            assert (Path(tmp) / "pulled" / ".hidden").read_text() == "remote\n"
            assert stat.S_IMODE((Path(tmp) / "pulled" / "run.sh").stat().st_mode) == 0o755
            evidence["checks"].append("download")
            # Simulate automatic exit, without Service.pause saving a snapshot ID.
            sb.terminate(wait=True)
            resumed2 = service.up(Config(name=name), resume_only=True)
            assert resumed2["sandbox_id"] != resumed["sandbox_id"]
            for key in ("rex_url", "rex_hostname", "rex_host_id"):
                assert resumed2[key] == record[key], (key, record, resumed2)
            assert service.execute(name, ["cat", ".hidden"], capture=True)["stdout"] == "remote\n"
            evidence["checks"].append("resume an exit with no manual pause")
            evidence["final"] = resumed2
            expiry_name = name + "-expiry"
            try:
                expired = service.up(Config(name=expiry_name, timeout=120))
                checks = service.execute(
                    expiry_name,
                    ["bash", "-c", "echo expiry-proof > /workspace/marker"],
                    capture=True,
                )
                assert checks["exit_code"] == 0, checks
                _, expiry_sb = service.running(expiry_name)
                deadline = time.monotonic() + 140
                while expiry_sb.poll() is None and time.monotonic() < deadline:
                    time.sleep(1)
                assert expiry_sb.poll() is not None, "Lifetime timeout did not occur"
                restored = service.up(Config(name=expiry_name), resume_only=True)
                assert restored["sandbox_id"] != expired["sandbox_id"]
                for key in ("rex_url", "rex_hostname", "rex_host_id"):
                    assert restored[key] == expired[key], (key, expired, restored)
                check = service.execute(expiry_name, ["cat", "/workspace/marker"], capture=True)
                assert check["stdout"] == "expiry-proof\n", check
                evidence["checks"].append("actual lifetime expiry and snapshot recovery")
            finally:
                cleanup_test_box(service, expiry_name)
            success = True
    finally:
        try:
            cleanup_test_box(service, name)
        except Exception:
            success = False
            evidence["cleanup_error"] = "test box network logout or removal failed"
            raise
        finally:
            modal.Volume.objects.delete(volume, allow_missing=True)
            evidence["success"] = success
            evidence["cleanup"] = "owned sandbox removal attempted, volume deleted"
            Path("smoke-result.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
