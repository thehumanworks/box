"""Development environment lifecycle; snapshots are the only resume source."""

import os
import shlex
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import modal

from box.config import Config, name_value, remote_path
from box.state import APP_NAME, Registry, timestamp


def ssh_argv(record, *, interactive=True):
    host = record.get("host")
    if not host:
        raise ValueError("SSH is not ready; run box up to finish network setup")
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    if interactive:
        args.append("-t")
    args.extend(
        [f"root@{host}", f"cd -- {shlex.quote(record['config']['workdir'])} && exec bash -l"]
    )
    return args


def result(record):
    public = {k: v for k, v in record.items() if k not in {"config", "git_identity"}}
    public["workdir"] = record["config"]["workdir"]
    public["volumes"] = record["config"]["volumes"]
    if record.get("host") and record["status"] == "running" and record["setup"]["complete"]:
        public["ssh_command"] = shlex.join(ssh_argv(record))
    return public


def git_identity():
    identity = {}
    for key in ("user.name", "user.email"):
        p = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
        if p.returncode == 0:
            identity[key] = p.stdout.rstrip("\n")
        elif p.returncode != 1:
            raise ValueError("Cannot read local Git identity; check your git configuration")
    return identity


class Service:
    def __init__(self, registry=None):
        self.registry = registry if registry is not None else Registry()

    def require(self, name):
        name_value(name)
        record = self.registry.get(name)
        if record is None:
            raise ValueError(f"Unknown environment {name!r}. Create it with: box up {name}")
        self._setup_state(record)
        return record

    def sandbox(self, record):
        if not record.get("sandbox_id"):
            raise ValueError(
                "Environment creation did not return a sandbox. Remove its record and retry"
            )
        return modal.Sandbox.from_id(record["sandbox_id"])

    def status(self, name):
        record = self.require(name)
        if record.get("sandbox_id"):
            try:
                code = self.sandbox(record).poll()
                record["status"] = (
                    "paused"
                    if code is not None
                    else "running"
                    if record["setup"]["complete"]
                    else "setup-incomplete"
                )
                record["exit_code"] = code
            except modal.exception.NotFoundError:
                record["status"] = "unavailable"
                record["error"] = "Sandbox no longer exists; resume may be unavailable"
        return result(record)

    def list(self):
        return [self.status(record["name"]) for record in self.registry.all()]

    def _volumes(self, config):
        volumes = {}
        for path, name in config.volumes.items():
            volume = modal.Volume.from_name(name, create_if_missing=True, version=2)
            # Hydration validates the requested version against existing volumes.
            volume.info()
            volumes[path] = volume
        return volumes

    def up(self, config, *, resume_only=False):
        from box.environment import ENTRYPOINT, build_image, start_network

        config.validate()
        name = config.name
        with self.registry.lock(name):
            previous = self.registry.get(name)
            if resume_only and previous is None:
                raise ValueError(f"Unknown environment {name!r}; nothing to resume")
            if previous:
                self._setup_state(previous)
                config = Config(**previous["config"]).validate()
                try:
                    old = self.sandbox(previous)
                    live = old.poll() is None
                except modal.exception.NotFoundError:
                    old, live = None, False
                if live:
                    if previous["setup"]["complete"]:
                        # Refresh an already-initialized box without erasing its
                        # durable setup checkpoint if a connectivity check fails.
                        self._ensure_workdir(old, config.workdir)
                        network = start_network(
                            old,
                            previous["hostname"],
                            auth_key=os.environ.get("TAILSCALE_AUTH_KEY"),
                            secret_name=config.tailscale_secret,
                        )
                        previous.update(network, status="running")
                        previous.pop("error", None)
                        self.registry.save(previous)
                        return result(previous)
                    return self._complete_setup(previous, old, config)
                # Never silently replace a missing/failed snapshot with an empty image.
                try:
                    if previous.get("snapshot_id"):
                        image = modal.Image.from_id(previous["snapshot_id"])
                    elif old is not None:
                        image = old._experimental_get_exit_snapshot(timeout=300)
                    else:
                        raise ValueError("Sandbox and saved exit snapshot are unavailable")
                except Exception as exc:
                    raise ValueError(
                        f"Cannot resume {name}: its exit snapshot is unavailable. "
                        "No replacement was created. Snapshots expire after 30 days."
                    ) from exc
                record = dict(previous)
                record["previous_sandbox_id"] = previous["sandbox_id"]
                record["previous_snapshot_id"] = image.object_id
            else:
                # Validate inputs before creating a billable sandbox. Persist the
                # initial author identity so recovery on another client won't change it.
                for clone in config.clones:
                    if not Path(clone["source"]).is_dir():
                        raise ValueError(f"Clone source is not a directory: {clone['source']}")
                identity = git_identity()
                image = build_image()
                record = {
                    "name": name,
                    "config": config.to_dict(),
                    "created_at": timestamp(),
                    "hostname": f"box-{name}-{uuid.uuid4().hex[:8]}",
                    "cloned": [],
                    "git_identity": identity,
                    "setup": {
                        "version": 1,
                        "complete": False,
                        "phase": "workdir",
                        "git_applied": False,
                    },
                    "snapshot_on_exit": True,
                }
            volumes = self._volumes(config)
            app = modal.App.lookup(APP_NAME, create_if_missing=True)
            # Modal builds the image lazily when creating the sandbox.
            with modal.enable_output():
                sb = modal.Sandbox.create(
                    *ENTRYPOINT,
                    app=app,
                    image=image,
                    timeout=config.timeout,
                    idle_timeout=config.idle_timeout,
                    cpu=config.cpu,
                    memory=config.memory,
                    region=config.region,
                    volumes=volumes,
                    secrets=[modal.Secret.from_name(s) for s in config.secrets],
                    experimental_options={"enable_exit_snapshot": True},
                    tags={"box.name": name, "box.managed": "v1"},
                )
            record.update(sandbox_id=sb.object_id, status="starting")
            record.pop("snapshot_id", None)
            for key in ("host", "tailscale_ip", "rex_url", "rex_hostname", "rex_host_id"):
                record.pop(key, None)
            # _complete_setup's first I/O saves this handle before any remote exec.
            return self._complete_setup(record, sb, config, stop_on_failure=previous is None)

    @staticmethod
    def _setup_state(record):
        """Upgrade legacy checkpoints in memory; only lifecycle operations save them."""
        record.setdefault("cloned", [])
        if "setup" not in record:
            clones_done = all(
                clone["destination"] in record.get("cloned", [])
                for clone in record["config"]["clones"]
            )
            # A live process is NOT evidence of readiness. Legacy starting/failed
            # records must run setup even when no clones were configured.
            complete = (
                record.get("status") in {"running", "paused"}
                and bool(record.get("host"))
                and clones_done
            )
            record["setup"] = {
                "version": 1,
                "complete": complete,
                "phase": "ready" if complete else "unknown",
                "git_applied": complete,
            }
        if record["setup"].get("version") != 1:
            raise ValueError("Unsupported box setup version; upgrade Box before using this record")
        return record["setup"]

    def _complete_setup(self, record, sb, config, *, stop_on_failure=False):
        """The single setup path for create, live recovery and snapshot resume."""
        from box.environment import start_network
        from box.transfer import copy_to

        setup = self._setup_state(record)

        def checkpoint(phase):
            setup.update(complete=False, phase=phase)
            record["status"] = "starting"
            record.pop("error", None)
            # Save the sandbox handle and phase BEFORE each external side effect.
            self.registry.save(record)

        try:
            checkpoint("workdir")
            self._ensure_workdir(sb, config.workdir)
            checkpoint("clones")
            for clone in config.clones:
                destination = clone["destination"]
                if destination in record["cloned"]:
                    continue
                source = Path(clone["source"])
                if not source.is_dir():
                    raise ValueError(
                        f"Initial clone source is unavailable on this client: {source}. "
                        f"Setup is incomplete; run box up {config.name} on the original client. "
                        f"Inspect with: box exec --cwd / {config.name} -- ls -la"
                    )
                try:
                    # Never merge a partial/ambiguous initial copy into user files.
                    # copy_to refuses nonempty destinations; a completed but
                    # uncheckpointed copy must be inspected, not guessed complete.
                    copy_to(
                        sb,
                        source,
                        destination,
                        exclude=tuple(clone.get("exclude", [])),
                        mounts=tuple(config.volumes),
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Initial clone to {destination} was not confirmed complete: {exc}. "
                        "Existing files will not be overwritten automatically. "
                        f"Inspect with: box exec --cwd / {config.name} -- ls -la. "
                        "Move any unconfirmed destination aside before retrying box up."
                    ) from exc
                record["cloned"].append(destination)
                self.registry.save(record)

            checkpoint("git")
            if not setup["git_applied"]:
                if "git_identity" not in record:
                    # Legacy interrupted records did not save the initial identity.
                    record["git_identity"] = git_identity()
                    self.registry.save(record)
                for key, value in record["git_identity"].items():
                    proc = sb.exec(
                        "git", "config", "--file", "/root/.gitconfig", key, value, workdir="/"
                    )
                    if proc.wait() != 0:
                        raise ValueError("Cannot apply Git author identity")
                setup["git_applied"] = True
                self.registry.save(record)

            checkpoint("network")
            record.update(
                start_network(
                    sb,
                    record["hostname"],
                    auth_key=os.environ.get("TAILSCALE_AUTH_KEY"),
                    secret_name=config.tailscale_secret,
                )
            )
            setup.update(complete=True, phase="ready")
            record["status"] = "running"
            self.registry.save(record)
            return result(record)
        except BaseException:
            setup["complete"] = False
            record["status"] = "setup-failed"
            record["error"] = (
                f"Setup incomplete at {setup['phase']}; filesystem retained. "
                f"Retry box up {config.name}; inspect a live box with "
                f"box exec --cwd / {config.name} -- ls -la."
            )
            try:
                # A failed first creation stops billing and retains its snapshot.
                # Reattaching or restoring a user's box must not kill their processes.
                if stop_on_failure:
                    sb.terminate(wait=True)
            finally:
                self.registry.save(record)
            raise

    def pause(self, name):
        with self.registry.lock(name):
            record = self.require(name)
            sb = self.sandbox(record)
            sb.terminate(wait=True)
            record["status"] = "paused"
            self.registry.save(record)
            try:
                snapshot = sb._experimental_get_exit_snapshot(timeout=300)
            except Exception as exc:
                raise ValueError(
                    f"{name} is stopped; snapshot is pending or failed. "
                    "Its sandbox ID is retained; retry box resume."
                ) from exc
            record["snapshot_id"] = snapshot.object_id
            self.registry.save(record)
            return result(record)

    def remove(self, name):
        with self.registry.lock(name):
            record = self.require(name)
            if record.get("sandbox_id"):
                try:
                    self.sandbox(record).terminate(wait=True)
                except modal.exception.NotFoundError:
                    pass
            self.registry.delete(name)
            return {
                "name": name,
                "status": "removed",
                "sandbox_id": record.get("sandbox_id"),
                "retained_volumes": record["config"]["volumes"],
            }

    @staticmethod
    def _ensure_workdir(sb, workdir):
        proc = sb.exec("mkdir", "-p", "--", workdir, workdir="/")
        if proc.wait() != 0:
            raise ValueError("Cannot create the remote working directory")

    def running(self, name, *, allow_incomplete=False):
        record = self.require(name)
        sb = self.sandbox(record)
        if sb.poll() is not None:
            raise ValueError(f"{name} is paused. Resume it with: box resume {name}")
        if not record["setup"]["complete"]:
            message = (
                f"{name} setup is incomplete (phase: {record['setup']['phase']}). "
                f"Run box up {name} to finish, or inspect with "
                f"box exec --cwd / {name} -- ls -la."
            )
            if not allow_incomplete:
                raise ValueError(message)
            print(f"box: {message}", file=sys.stderr)
        return record, sb

    def execute(
        self, name, command, *, capture=False, timeout=3600, cwd=None, max_output_bytes=None
    ):
        if max_output_bytes is not None and (
            not capture or type(max_output_bytes) is not int or max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes requires capture=True and a positive integer")
        if cwd is not None:
            cwd = remote_path(cwd, allow_root=True)
        record, sb = self.running(name, allow_incomplete=cwd is not None)
        workdir = cwd if cwd is not None else record["config"]["workdir"]
        # Only setup owns directory creation. Let Modal chdir before launching
        # anything; argv remains literal while loading the Home Manager profile.
        proc = sb.exec(
            "/bin/bash",
            "-c",
            '. /etc/profile.d/box.sh && exec -- "$@"',
            "box-exec",
            *command,
            workdir=workdir,
            timeout=timeout,
        )
        # Drain both streams concurrently so a full stderr pipe cannot deadlock stdout.
        if capture:
            with ThreadPoolExecutor(max_workers=2) as pool:
                out = pool.submit(self._capture, proc.stdout, max_output_bytes)
                err = pool.submit(self._capture, proc.stderr, max_output_bytes)
                code = proc.wait()
                stdout, stdout_truncated = out.result()
                stderr, stderr_truncated = err.result()
                hint = self._cwd_failure_hint(code, stderr, name, workdir)
                value = {"exit_code": code, "stdout": stdout, "stderr": stderr + hint}
                if max_output_bytes is not None:
                    encoded_stderr = value["stderr"].encode("utf-8")
                    if len(encoded_stderr) > max_output_bytes:
                        value["stderr"] = encoded_stderr[:max_output_bytes].decode(
                            "utf-8", errors="ignore"
                        )
                        stderr_truncated = True
                    value.update(
                        stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated
                    )
                return value

        def drain(stream, target):
            tail = ""
            for line in stream:
                target.write(line)
                target.flush()
                tail = (tail + line)[-8192:]
            return tail

        with ThreadPoolExecutor(max_workers=2) as pool:
            out = pool.submit(drain, proc.stdout, sys.stdout)
            err = pool.submit(drain, proc.stderr, sys.stderr)
            code = proc.wait()
            out.result()
            hint = self._cwd_failure_hint(code, err.result(), name, workdir)
            if hint:
                print(hint, end="", file=sys.stderr)
        return {"exit_code": code}

    @staticmethod
    def _capture(stream, limit):
        if limit is None:
            return stream.read(), False
        output, truncated = bytearray(), False
        # Modal yields bounded chunks (not lines). Continue draining after the
        # cap so large output cannot exhaust memory or block the remote process.
        for chunk in stream:
            data = chunk.encode("utf-8")
            remaining = limit - len(output)
            output.extend(data[:remaining])
            truncated |= len(data) > remaining
        # Drop an incomplete final UTF-8 codepoint instead of replacing it with
        # three bytes that could exceed the configured byte limit.
        return output.decode("utf-8", errors="ignore"), truncated

    @staticmethod
    def _cwd_failure_hint(code, stderr, name, workdir):
        # Modal 1.6 returns a process with exit 128 when initial chdir fails.
        # Add advice without replaying the command or changing its exit code.
        if code != 128 or "failed to find initial working directory" not in stderr:
            return ""
        return (
            f"\nbox: Cannot enter remote working directory {workdir!r}. "
            f"Use box exec --cwd / {name} -- ls -la to inspect it. "
            f"Run box up {name} to repair the configured directory; "
            "an explicit --cwd must already exist. No directory was created by exec.\n"
        )
