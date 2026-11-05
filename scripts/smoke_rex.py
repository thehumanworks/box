"""Opt-in Rex lifecycle check using only a uniquely named test box."""

import json
import subprocess
import time
import uuid
from pathlib import Path

from smoke import cleanup_test_box, verify_rex, verify_rex_environment

from box.config import Config
from box.service import Service


def main():
    name = "box-rex-smoke-" + uuid.uuid4().hex[:8]
    service = Service()
    evidence = {"name": name, "checks": [], "success": False, "records": {}}
    marker = "rex-snapshot-proof-" + uuid.uuid4().hex

    def resume(previous, phase, *, terminal=True):
        restored = service.up(Config(name=name), resume_only=True)
        assert restored["sandbox_id"] != previous["sandbox_id"], restored
        for key in ("rex_url", "rex_hostname", "rex_host_id", "host"):
            assert restored[key] == initial[key], (key, initial, restored)
        check = service.execute(name, ["cat", "/workspace/marker"], capture=True)
        assert check["exit_code"] == 0 and check["stdout"] == marker, check
        evidence["records"][phase] = restored
        evidence["checks"].append(f"Rex and SSH identities plus marker preserved ({phase})")
        if terminal:
            verify_rex(restored, evidence, phase=phase)
        print(json.dumps({"verified": phase, "sandbox_id": restored["sandbox_id"]}), flush=True)
        return restored

    try:
        version = subprocess.run(
            ["rex", "version"], capture_output=True, text=True, timeout=15, check=True
        )
        evidence["local_rex_version"] = version.stdout.strip()
        initial = service.up(Config(name=name, timeout=120))
        evidence["records"]["initial"] = initial
        print(json.dumps({"created": initial}), flush=True)
        verify_rex(initial, evidence, phase="initial")
        verify_rex_environment(service, name, evidence)
        written = service.execute(
            name,
            [
                "python",
                "-c",
                "import sys; from pathlib import Path; "
                "Path('/workspace/marker').write_text(sys.argv[1])",
                marker,
            ],
            capture=True,
        )
        assert written["exit_code"] == 0, written

        paused = service.pause(name)
        evidence["snapshot_id"] = paused["snapshot_id"]
        restored = resume(initial, "manual pause")
        _, sandbox = service.running(name)
        sandbox.terminate(wait=True)
        restored = resume(restored, "direct termination", terminal=False)

        _, sandbox = service.running(name)
        started = time.monotonic()
        deadline = started + 145
        while (exit_code := sandbox.poll()) is None and time.monotonic() < deadline:
            time.sleep(1)
        # Modal maps an actual lifetime timeout to 124; an early crash must fail.
        assert exit_code == 124, ("Expected lifetime timeout", exit_code)
        evidence["expiry_wait_seconds"] = round(time.monotonic() - started, 2)
        evidence["checks"].append("actual 120-second lifetime expiry")
        resume(restored, "lifetime expiry")
        evidence["success"] = True
    except Exception as error:
        evidence["error"] = str(error)
        raise
    finally:
        try:
            cleanup_test_box(service, name)
            evidence["cleanup"] = "test box cleanup completed"
        except Exception:
            evidence["success"] = False
            evidence["cleanup"] = "test-owned network logout or sandbox removal failed"
            raise
        finally:
            Path("rex-smoke-result.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
