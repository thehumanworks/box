"""Small, strict, portable project configuration. No credentials belong here."""

import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath


def name_value(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ValueError(
            "Name must be 1–40 lowercase letters, digits or hyphens, starting with a letter"
        )
    return value


def remote_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
    ):
        raise ValueError("Remote paths must be absolute")
    if ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) == "/":
        raise ValueError("Remote paths must not be / or contain '..'")
    return str(PurePosixPath(value))


@dataclass
class Config:
    name: str = "dev"
    workdir: str = "/workspace"
    cpu: float = 2
    memory: int = 4096
    timeout: int = 86400
    idle_timeout: int | None = None
    tailscale_secret: str = "tailscale"
    region: str | None = None
    clones: list[dict] = field(default_factory=list)
    volumes: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=lambda: ["1password"])

    def validate(self):
        name_value(self.name)
        self.workdir = remote_path(self.workdir)
        for key in ("cpu", "memory", "timeout"):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{key} must be a positive number")
        if not isinstance(self.memory, int) or not isinstance(self.timeout, int):
            raise ValueError("memory and timeout must be integers")
        if self.timeout > 86400:
            raise ValueError("timeout must be at most 86400 seconds (Modal sandbox limit)")
        if self.idle_timeout is not None and (
            type(self.idle_timeout) is not int or self.idle_timeout <= 0
        ):
            raise ValueError("idle_timeout must be a positive integer")
        if not isinstance(self.tailscale_secret, str) or not self.tailscale_secret:
            raise ValueError("tailscale_secret must be a Modal secret name")
        if self.region is not None and (not isinstance(self.region, str) or not self.region):
            raise ValueError("region must be a nonempty string")
        if not isinstance(self.secrets, list) or any(
            not isinstance(x, str) or not x for x in self.secrets
        ):
            raise ValueError("secrets must be an array of Modal secret names")
        if not isinstance(self.volumes, dict):
            raise ValueError("volumes must map absolute mount paths to volume names")
        mounts = []
        normalized_volumes = {}
        for path, volume in self.volumes.items():
            path = remote_path(path)
            if path != "/workspace" and not path.startswith(("/workspace/", "/mnt/", "/data/")):
                raise ValueError(
                    "Volume mounts must be /workspace or under /workspace, /mnt or /data"
                )
            if not isinstance(volume, str) or not volume:
                raise ValueError("Volume names must be nonempty strings")
            if any(
                path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in mounts
            ):
                raise ValueError("Volume mount paths must not overlap")
            mounts.append(path)
            normalized_volumes[path] = volume
        self.volumes = normalized_volumes
        if not isinstance(self.clones, list):
            raise ValueError("clones must be an array of source/destination tables")
        destinations = []
        for clone in self.clones:
            if not isinstance(clone, dict) or set(clone) - {"source", "destination", "exclude"}:
                raise ValueError("Each clone accepts source, destination and optional exclude")
            if not isinstance(clone.get("source"), str) or not clone["source"]:
                raise ValueError("Each clone needs a local source directory")
            dest = remote_path(clone.get("destination", self.workdir))
            if any(
                dest == p or dest.startswith(p + "/") or p.startswith(dest + "/")
                for p in destinations
            ):
                raise ValueError("Clone destinations must not overlap")
            destinations.append(dest)
            clone["destination"] = dest
            patterns = clone.get("exclude", [])
            if not isinstance(patterns, list) or any(not isinstance(x, str) for x in patterns):
                raise ValueError("clone.exclude must be an array of patterns")
        return self

    def to_dict(self):
        return asdict(self)


def load_config(path: Path | None = None) -> Config:
    path = path or Path("box.toml")
    if not path.exists():
        if path != Path("box.toml"):
            raise ValueError(f"Configuration does not exist: {path}")
        return Config()
    data = tomllib.loads(path.read_text())
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    cfg = Config(**data).validate()
    for clone in cfg.clones:
        source = Path(clone["source"]).expanduser()
        clone["source"] = str((path.resolve().parent / source).resolve())
        clone.setdefault("destination", cfg.workdir)
    return cfg
