"""Exercise the deployed HTTPS service against an existing box, never mocks.

Uses the protected proxy credential file written by deploy_remote.py. No Modal
API credentials are needed by this client. Only test-owned /tmp files are written.
"""

import argparse
import json
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--name", required=True, help="Existing ready box to test.")
    parser.add_argument("--output", type=Path, required=True, help="Non-secret JSON evidence file.")
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    endpoint = deployment["url"].rstrip("/")
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".modal.run")
        or not url.hostname.startswith(f"{deployment['workspace']}--{deployment['app']}-")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path
    ):
        raise ValueError("Expected this deployment's HTTPS Modal URL; refusing to send credentials")
    credentials_file = Path(deployment["credentials_file"])
    if credentials_file.is_symlink() or credentials_file.stat().st_mode & 0o077:
        raise ValueError("Proxy credential file must be private and not a symlink")
    credentials = json.loads(credentials_file.read_text())
    if (
        credentials["workspace"] != deployment["workspace"]
        or credentials["environment"] != deployment["environment"]
    ):
        raise ValueError("Proxy credentials do not match the deployment")
    headers = {"Modal-Key": credentials["token_id"], "Modal-Secret": credentials["token_secret"]}
    checks = []
    marker = "/tmp/box-remote-smoke-" + uuid.uuid4().hex
    missing = marker + "-absent-directory"
    timeout_marker = marker + "-timeout-finished"
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "app": deployment["app"],
        "app_id": deployment["app_id"],
        "workspace": deployment["workspace"],
        "environment": deployment["environment"],
        "url": endpoint,
        "box": args.name,
        "checks": checks,
        "passed": False,
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    with httpx.Client(
        verify=ssl.create_default_context(), timeout=125, follow_redirects=False
    ) as client:

        def send(label, method, path, expected, *, body=None, auth=None):
            start = time.monotonic()
            result = client.request(
                method,
                endpoint + path,
                json=body,
                headers=headers if auth is None else auth,
            )
            duration = round(time.monotonic() - start, 3)
            if result.status_code != expected:
                raise AssertionError(f"{label}: HTTP {result.status_code}, expected {expected}")
            checks.append({"check": label, "http_status": result.status_code, "seconds": duration})
            print(f"PASS {label}: HTTP {result.status_code}, {duration}s", flush=True)
            save()
            return (
                result.json()
                if "application/json" in result.headers.get("content-type", "")
                else None
            )

        def payload(command, **changes):
            return {
                "name": args.name,
                "command": command,
                "request_id": uuid.uuid4().hex,
                **changes,
            }

        def execute(label, command, expected=200, **changes):
            return send(label, "POST", "/exec", expected, body=payload(command, **changes))

        def poll_to_completion(call_id, label):
            deadline = time.monotonic() + 100
            pending = 0
            while time.monotonic() < deadline:
                result = client.get(endpoint + "/poll/" + call_id, headers=headers)
                assert result.status_code in (200, 202), f"{label}: poll HTTP {result.status_code}"
                value = result.json()
                if result.status_code == 200:
                    checks.append(
                        {
                            "check": label,
                            "http_status": 200,
                            "pending_polls": pending,
                            "call_id": call_id,
                            "status": value["status"],
                        }
                    )
                    print(f"PASS {label}: {value['status']}, {pending} pending polls", flush=True)
                    save()
                    return value, pending
                assert value["status"] == "pending"
                pending += 1
                time.sleep(0.4)
            raise AssertionError(f"{label}: did not finish within deadline")

        try:
            # Authentication must apply to every endpoint, including result reads.
            for method, path in [
                ("POST", "/exec"),
                ("POST", "/spawn"),
                ("GET", "/poll/fc-unknown"),
            ]:
                send("unauthenticated " + path, method, path, 401, auth={})
            send(
                "invalid credentials",
                "POST",
                "/exec",
                401,
                auth={"Modal-Key": "wk-invalid", "Modal-Secret": "ws-invalid"},
            )

            first = execute("nix version (first)", ["nix", "--version"])
            assert first["status"] == "completed" and first["exit_code"] == 0
            assert "Determinate Nix" in first["stdout"]
            second = execute("nix version (warm)", ["nix", "--version"])
            assert second["stdout"] == first["stdout"]
            value = execute(
                "cwd, literal argv, streams and exit",
                [
                    "python3",
                    "-c",
                    "import os,sys,json; print(json.dumps([os.getcwd(),sys.argv[1:]])); "
                    "print('stderr-ok',file=sys.stderr); sys.exit(23)",
                    "a b",
                    "$(false)",
                    "",
                ],
                cwd="/",
            )
            assert value["exit_code"] == 23 and value["stderr"] == "stderr-ok\n"
            assert json.loads(value["stdout"]) == ["/", ["a b", "$(false)", ""]]

            for label, changes in [
                ("invalid empty argv", {"command": []}),
                ("invalid cwd", {"cwd": "relative"}),
                ("sync timeout requires spawn", {"timeout": 91}),
            ]:
                send(label, "POST", "/exec", 422, body={**payload(["true"]), **changes})
            unknown = execute("unknown box", ["true"], 409, name="absent-" + uuid.uuid4().hex[:12])
            assert unknown["error"]["code"] == "box_unavailable"
            value = execute("missing cwd is not created", ["true"], cwd=missing)
            assert value["exit_code"] != 0
            check = execute(
                "verify missing cwd remains absent",
                [
                    "python3",
                    "-c",
                    "import os,sys; assert not os.path.exists(sys.argv[1]); print(os.getcwd())",
                    missing,
                ],
            )
            assert check["exit_code"] == 0

            # Same ID + same payload must not dispatch the side effect twice.
            duplicate = payload(
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                    "f=p.open('a'); f.write('once\\n'); f.close(); "
                    "print(len(p.read_text().splitlines()))",
                    marker,
                ]
            )
            original = send("idempotent original", "POST", "/exec", 200, body=duplicate)
            again = send("idempotent repeat", "POST", "/exec", 200, body=duplicate)
            assert original == again and again["stdout"] == "1\n"
            actual = execute(
                "side effect happened once",
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
                    marker,
                ],
            )
            assert actual["stdout"] == "once\n\n"
            conflict = send(
                "idempotency conflict",
                "POST",
                "/exec",
                409,
                body={**duplicate, "command": ["true"]},
            )
            assert conflict["error"]["code"] == "request_conflict"

            timed_out = execute(
                "sandbox execution timeout",
                [
                    "python3",
                    "-c",
                    "import time,sys; from pathlib import Path; time.sleep(6); "
                    "Path(sys.argv[1]).write_text('not killed')",
                    timeout_marker,
                ],
                expected=504,
                timeout=2,
            )
            assert timed_out["error"]["code"] == "exec_timeout"

            spawned = send(
                "spawn returns handle",
                "POST",
                "/spawn",
                202,
                body=payload(
                    [
                        "python3",
                        "-c",
                        "import time; time.sleep(6); print('background-complete')",
                    ],
                    timeout=120,
                ),
            )
            terminal, pending = poll_to_completion(spawned["call_id"], "background completion")
            assert pending > 0 and terminal["result"]["stdout"] == "background-complete\n"
            repeated = send("repeat polling", "GET", spawned["poll_url"], 200)
            assert repeated == terminal
            failed = send(
                "spawn invalid box",
                "POST",
                "/spawn",
                202,
                body=payload(["true"], name="absent-" + uuid.uuid4().hex[:12]),
            )
            terminal, _ = poll_to_completion(failed["call_id"], "background failure")
            assert terminal["status"] == "failed"
            assert terminal["result"]["error"]["code"] == "box_unavailable"
            send(
                "unknown call cannot read other results", "GET", "/poll/fc-" + uuid.uuid4().hex, 404
            )

            # Concurrent requests without a client-supplied ID must get distinct
            # Modal input identities, not a shared ASGI container identity.
            anonymous = {
                "name": args.name,
                "command": [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; "
                    "f=Path(sys.argv[1]).open('a'); f.write('anonymous\\n'); f.close(); "
                    "print('anonymous-complete')",
                    marker,
                ],
            }

            def submit_anonymous(_index):
                return client.post(endpoint + "/spawn", headers=headers, json=anonymous)

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(submit_anonymous, range(2)))
            assert all(item.status_code == 202 for item in responses)
            calls = [item.json() for item in responses]
            assert len({item["request_id"] for item in calls}) == 2
            assert len({item["call_id"] for item in calls}) == 2
            for item in calls:
                assert item["request_id"].startswith("fc-")
                assert item["request_id"] != item["call_id"]
                terminal, _ = poll_to_completion(item["call_id"], "concurrent implicit request ID")
                assert terminal["result"]["stdout"] == "anonymous-complete\n"
            actual = execute(
                "concurrent requests both executed",
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; "
                    "assert Path(sys.argv[1]).read_text().splitlines().count('anonymous') == 2; "
                    "assert not Path(sys.argv[2]).exists()",
                    marker,
                    timeout_marker,
                ],
            )
            assert actual["exit_code"] == 0, (
                "Anonymous requests collided or timed-out process survived"
            )

            output = execute(
                "bounded output on both streams",
                [
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('x'*(1048576+100)); "
                    "sys.stderr.write('y'*(1048576+100))",
                ],
            )
            assert output["exit_code"] == 0
            assert len(output["stdout"].encode()) == len(output["stderr"].encode()) == 1048576
            assert output["stdout_truncated"] and output["stderr_truncated"]
        finally:
            cleanup = execute(
                "cleanup test-owned files",
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; import sys; "
                    "[Path(p).unlink(missing_ok=True) for p in sys.argv[1:]]",
                    marker,
                    timeout_marker,
                ],
            )
            assert cleanup["exit_code"] == 0
            report["finished_at"] = datetime.now(UTC).isoformat()
            save()
        report["passed"] = True
        save()
    print(f"Verified {len(checks)} live HTTP checks. Evidence: {args.output}")


if __name__ == "__main__":
    main()
