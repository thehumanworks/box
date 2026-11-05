"""Real CLI→HTTPS→Modal→sandbox checks, using an isolated temporary sandbox.

Run under dotenvx for fixture provisioning only. CLI subprocesses receive invalid
Modal API credentials to prove execution uses the HTTP proxy credential file.
No existing box is resumed, repaired, overwritten or removed.
"""

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import modal

from box.config import Config
from box.modal_transport import connect
from box.remote_client import deployment_file, read_json
from box.state import REGISTRY_NAME


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--box", type=Path, default=Path(".venv/bin/box"))
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/cli-remote-verification.json"))
    args = parser.parse_args()
    manifest = deployment_file(args.deployment).resolve()
    deployment = read_json(manifest)
    executable = args.box.resolve()
    connect()
    workspace = modal.Workspace.from_context()
    workspace.hydrate()
    assert workspace.name == deployment["workspace"], "Fixture credentials target another workspace"
    environment = deployment["environment"]
    runs = modal.Dict.from_name(REGISTRY_NAME, environment_name=environment)
    name = "cli-smoke-" + uuid.uuid4().hex[:12]
    key = "box:" + name
    child_env = dict(
        os.environ, MODAL_TOKEN_ID="ak-invalid-cli-test", MODAL_TOKEN_SECRET="as-invalid-cli-test"
    )
    checks = []
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "workspace": workspace.name,
        "environment": environment,
        "url": deployment["url"],
        "box": name,
        "fixture": "temporary real Modal sandbox; no secrets mounted",
        "cli_api_credentials": "deliberately invalid",
        "checks": checks,
        "passed": False,
    }

    def invoke(label, arguments, expected=0, *, human=False):
        started = time.monotonic()
        proc = subprocess.run(
            [
                str(executable),
                "--remote-deployment",
                str(manifest),
                "--human" if human else "--json",
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=child_env,
            timeout=150,
        )
        elapsed = round(time.monotonic() - started, 3)
        assert proc.returncode == expected, f"{label}: exit {proc.returncode}, expected {expected}"
        value = proc.stdout if human else json.loads(proc.stdout)
        if not human:
            assert proc.stderr == "", f"{label}: JSON diagnostics leaked to stderr"
        checks.append({"check": label, "exit_code": proc.returncode, "seconds": elapsed})
        print(f"PASS {label}: exit {proc.returncode}, {elapsed}s", flush=True)
        return value, proc.stderr

    image = modal.Image.debian_slim(python_version="3.14").run_commands(
        "mkdir -p /workspace && touch /etc/profile.d/box.sh"
    )
    app = modal.App()
    with app.run(environment_name=environment):
        sb = modal.Sandbox.create("sleep", "600", image=image, timeout=600, app=app)
        report["sandbox_id"] = sb.object_id
        registered = False
        try:
            record = {
                "name": name,
                "sandbox_id": sb.object_id,
                "status": "running",
                "config": Config(name=name).to_dict(),
                "cloned": [],
                "setup": {"version": 1, "complete": True, "phase": "ready", "git_applied": True},
            }
            registered = runs.put(key, record, skip_if_exists=True)
            assert registered, "Refusing to replace an existing box"
            value, _ = invoke(
                "HTTPS exec with invalid SDK credentials",
                ["exec", name, "--", "python3", "--version"],
            )
            assert value["exit_code"] == 0 and value["stdout"].startswith("Python 3.14")
            value, _ = invoke("warm HTTPS exec", ["exec", name, "--", "pwd"])
            assert value["stdout"] == "/workspace\n"
            command = [
                "python3",
                "-c",
                "import os,sys,json; print(json.dumps([os.getcwd(),sys.argv[1:]])); "
                "print('stderr-ok',file=sys.stderr); sys.exit(23)",
                "a b",
                "$(false)",
                "",
            ]
            value, _ = invoke(
                "JSON argv, cwd, stderr and exit", ["exec", "-w", "/", name, "--", *command], 23
            )
            assert json.loads(value["stdout"]) == ["/", ["a b", "$(false)", ""]]
            assert value["stderr"] == "stderr-ok\n"
            stdout, stderr = invoke(
                "human buffered streams",
                [
                    "exec",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "import sys; print('out'); print('err',file=sys.stderr); sys.exit(23)",
                ],
                23,
                human=True,
            )
            assert stdout == "out\n" and stderr == "err\n"
            value, _ = invoke(
                "timeout becomes exit 124",
                [
                    "exec",
                    "--timeout",
                    "1",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "import time; time.sleep(5)",
                ],
                124,
            )
            assert value["error"]["code"] == "exec_timeout"
            submitted, _ = invoke(
                "spawn returns call ID",
                [
                    "spawn",
                    "--timeout",
                    "60",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "import time; time.sleep(8); print('done')",
                ],
            )
            call_id = submitted["call_id"]
            pending, _ = invoke("pending poll exits 75", ["poll", call_id], 75)
            assert pending["status"] == "pending"
            pending, _ = invoke(
                "wait deadline does not cancel job",
                ["poll", "--wait", "--timeout", "2", call_id],
                75,
            )
            assert pending["status"] == "pending"
            finished, _ = invoke(
                "wait reaches completed result", ["poll", "--wait", "--timeout", "60", call_id]
            )
            assert finished["result"]["stdout"] == "done\n"
            repeated, _ = invoke("repeat polling is stable", ["poll", call_id])
            assert repeated == finished
            human_id, _ = invoke(
                "human spawn prints only handle",
                [
                    "spawn",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "print('background-exit'); raise SystemExit(23)",
                ],
                human=True,
            )
            assert human_id.strip().startswith("fc-") and len(human_id.splitlines()) == 1
            stdout, stderr = invoke(
                "human poll preserves background exit",
                ["poll", "--wait", human_id.strip()],
                23,
                human=True,
            )
            assert stdout == "background-exit\n" and stderr == ""
            unknown, _ = invoke("unknown call is an error", ["poll", "fc-" + uuid.uuid4().hex], 1)
            assert unknown["error"]["code"] == "unknown_call"
            failed, _ = invoke(
                "spawn of unknown box remains observable",
                ["spawn", "absent-" + uuid.uuid4().hex[:8], "--", "true"],
            )
            result, _ = invoke(
                "poll exposes terminal execution failure", ["poll", "--wait", failed["call_id"]], 1
            )
            assert result["result"]["error"]["code"] == "box_unavailable"
            request_id = uuid.uuid4().hex
            command = [
                "python3",
                "-c",
                "from pathlib import Path; p=Path('/tmp/once'); "
                "p.write_text(p.read_text()+'x' if p.exists() else 'x'); print(p.read_text())",
            ]
            options = ["exec", "--request-id", request_id, name, "--", *command]
            original, _ = invoke("explicit idempotency ID", options)
            replay, _ = invoke("same ID does not reexecute", options)
            assert replay == original and replay["stdout"] == "x\n"
            value, _ = invoke(
                "verify side effect occurred once",
                [
                    "exec",
                    name,
                    "--",
                    "python3",
                    "-c",
                    "from pathlib import Path; print(Path('/tmp/once').read_text())",
                ],
            )
            assert value["stdout"] == "x\n"
            conflict, _ = invoke(
                "changed input with same ID fails",
                ["exec", "--request-id", request_id, name, "--", "true"],
                1,
            )
            assert conflict["error"]["code"] == "request_conflict"
            report["passed"] = True
        finally:
            sb.terminate()
            if registered:
                assert runs.get(key)["sandbox_id"] == sb.object_id, (
                    "Box record changed; refusing cleanup"
                )
                runs.pop(key)
            report["fixture_removed"] = runs.get(key) is None
            report["finished_at"] = datetime.now(UTC).isoformat()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Verified {len(checks)} live CLI checks; temporary sandbox/record removed. "
        f"Evidence: {args.output}"
    )


if __name__ == "__main__":
    main()
