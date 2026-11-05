"""Development environment lifecycle; snapshots are the only resume source."""

import os
import shlex
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import modal

from box.config import Config, name_value
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
    public = {k: v for k, v in record.items() if k != "config"}
    public["workdir"] = record["config"]["workdir"]
    public["volumes"] = record["config"]["volumes"]
    if record.get("host"):
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
                record["status"] = "running" if code is None else "paused"
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
        from box.transfer import copy_to

        config.validate()
        name = config.name
        with self.registry.lock(name):
            previous = self.registry.get(name)
            if resume_only and previous is None:
                raise ValueError(f"Unknown environment {name!r}; nothing to resume")
            if previous:
                config = Config(**previous["config"]).validate()
                old = self.sandbox(previous)
                if old.poll() is None:
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
                # Never silently replace a missing/failed snapshot with an empty image.
                try:
                    image = (
                        modal.Image.from_id(previous["snapshot_id"])
                        if previous.get("snapshot_id")
                        else old._experimental_get_exit_snapshot(timeout=300)
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Cannot resume {name}: its exit snapshot is unavailable. "
                        "No replacement was created. Snapshots expire after 30 days."
                    ) from exc
                record = dict(previous)
                record["previous_sandbox_id"] = previous["sandbox_id"]
                record["previous_snapshot_id"] = image.object_id
            else:
                # Validate all sources before creating a billable sandbox.
                for clone in config.clones:
                    if not Path(clone["source"]).is_dir():
                        raise ValueError(f"Clone source is not a directory: {clone['source']}")
                image = build_image()
                record = {
                    "name": name,
                    "config": config.to_dict(),
                    "created_at": timestamp(),
                    "hostname": f"box-{name}-{uuid.uuid4().hex[:8]}",
                    "cloned": [],
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
            record.pop("error", None)
            try:
                # Save the handle before networking/copy so interrupted setup is recoverable.
                self.registry.save(record)
                proc = sb.exec("mkdir", "-p", "--", config.workdir)
                if proc.wait() != 0:
                    raise ValueError("Cannot create the remote working directory")
                if previous is None:
                    for clone in config.clones:
                        copy_to(
                            sb,
                            Path(clone["source"]),
                            clone["destination"],
                            exclude=tuple(clone.get("exclude", [])),
                            mounts=tuple(config.volumes),
                        )
                        record["cloned"].append(clone["destination"])
                        self.registry.save(record)
                    for key, value in git_identity().items():
                        proc = sb.exec("git", "config", "--file", "/root/.gitconfig", key, value)
                        if proc.wait() != 0:
                            raise ValueError("Cannot apply Git author identity")
                record.update(
                    start_network(
                        sb,
                        record["hostname"],
                        auth_key=os.environ.get("TAILSCALE_AUTH_KEY"),
                        secret_name=config.tailscale_secret,
                    ),
                    status="running",
                )
                self.registry.save(record)
                return result(record)
            except BaseException:
                # Retain the filesystem and ID for recovery, including a partial copy.
                record["status"] = "setup-failed"
                record["error"] = (
                    "Setup failed; filesystem retained. Resume, then retry any failed clone."
                )
                try:
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

    def running(self, name):
        record = self.require(name)
        sb = self.sandbox(record)
        if sb.poll() is not None:
            raise ValueError(f"{name} is paused. Resume it with: box resume {name}")
        return record, sb

    def execute(self, name, command, *, capture=False, timeout=3600):
        record, sb = self.running(name)
        # Load the current Home Manager generation for command-line agents too.
        # Positional parameters preserve argv; user input is never shell source.
        proc = sb.exec(
            "/bin/bash",
            "-c",
            '. /etc/profile.d/box.sh && exec -- "$@"',
            "box-exec",
            *command,
            workdir=record["config"]["workdir"],
            timeout=timeout,
        )
        # Drain both streams concurrently so a full stderr pipe cannot deadlock stdout.
        if capture:
            with ThreadPoolExecutor(max_workers=2) as pool:
                out = pool.submit(proc.stdout.read)
                err = pool.submit(proc.stderr.read)
                code = proc.wait()
                return {"exit_code": code, "stdout": out.result(), "stderr": err.result()}
        import sys

        def drain(stream, target):
            for line in stream:
                target.write(line)
                target.flush()

        with ThreadPoolExecutor(max_workers=2) as pool:
            out = pool.submit(drain, proc.stdout, sys.stdout)
            err = pool.submit(drain, proc.stderr, sys.stderr)
            code = proc.wait()
            out.result()
            err.result()
        return {"exit_code": code}
