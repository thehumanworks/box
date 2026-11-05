"""One interface for people, scripts, and coding agents."""

import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import click
import modal

from box import __version__
from box.config import Config, load_config, name_value, remote_path
from box.service import Service, ssh_argv


class Context:
    def __init__(self, config_path, as_json):
        self.config_path = config_path
        self.json = as_json if as_json is not None else not sys.stdout.isatty()

    def config(self):
        return load_config(self.config_path)

    def name(self, name):
        return name_value(name or self.config().name)

    def emit(self, value):
        if self.json:
            click.echo(json.dumps(value, ensure_ascii=False))
        elif isinstance(value, list):
            if not value:
                click.echo("No environments. Start one with: box up")
            for row in value:
                click.echo(f"{row['name']:<24} {row['status']:<14} {row.get('host', '')}")
        elif "status" in value:
            click.echo(f"{value.get('name', 'box')}: {value['status']}")
            if value.get("ssh_command") and value["status"] == "running":
                click.echo(value["ssh_command"])
            if value.get("rex_url") and value["status"] == "running":
                click.echo(f"Rex: {value['rex_url']}")
        else:
            click.echo(json.dumps(value, indent=2, ensure_ascii=False))


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Project configuration (default: ./box.toml).",
)
@click.option(
    "--json/--human",
    "as_json",
    default=None,
    help="Structured results; default for non-terminal stdout. Put before the command.",
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx, config_path, as_json):
    """Portable development environments with Tailscale SSH.

    Start: box up dev --clone .    Connect: box ssh dev
    Pause: box pause dev          Continue: box resume dev

    Pausing saves files. Processes restart on resume. No prompt is needed for
    normal operations; destructive removal requires --yes in scripts.
    """
    ctx.obj = Context(config_path, as_json)


@cli.command()
@click.argument("name", required=False)
@click.option(
    "--clone",
    "sources",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Copy a local directory to /workspace/<directory-name> on first creation.",
)
@click.option(
    "--volume",
    "mounts",
    multiple=True,
    metavar="NAME:/PATH",
    help="Mount a named Modal v2 volume; retained when the box is removed.",
)
@click.option("--cpu", type=click.FloatRange(min=0, min_open=True))
@click.option("--memory", type=click.IntRange(min=1), help="Memory in MiB.")
@click.option(
    "--timeout", type=click.IntRange(min=1, max=86400), help="Lifetime in seconds, default 86400."
)
@click.option(
    "--idle-timeout",
    type=click.IntRange(min=1),
    help="Optional idle expiry. Tailscale SSH traffic may not reset Modal's idle timer.",
)
@click.option("--tailscale-secret", help="Modal secret containing TAILSCALE_AUTHKEY.")
@click.pass_obj
def up(ctx, name, sources, mounts, cpu, memory, timeout, idle_timeout, tailscale_secret):
    """Create, reconnect, or resume NAME. Existing boxes retain their saved configuration."""
    config = ctx.config()
    if name:
        config.name = name_value(name)
    for key, value in locals().copy().items():
        if (
            key in {"cpu", "memory", "timeout", "idle_timeout", "tailscale_secret"}
            and value is not None
        ):
            setattr(config, key, value)
    for source in sources:
        source = source.resolve()
        config.clones.append(
            {"source": str(source), "destination": f"{config.workdir}/{source.name}"}
        )
    for spec in mounts:
        volume, separator, mount = spec.partition(":")
        if not separator or not volume:
            raise ValueError("--volume expects NAME:/absolute/mount/path")
        config.volumes[remote_path(mount)] = volume
    service = Service()
    if service.registry.get(config.name) and (
        sources
        or mounts
        or any(x is not None for x in (cpu, memory, timeout, idle_timeout, tailscale_secret))
    ):
        raise ValueError(
            "This box already exists. Use box clone for more files; "
            "resume uses its saved configuration"
        )
    with redirect_stdout(sys.stderr if ctx.json else sys.stdout):
        value = service.up(config)
    ctx.emit(value)


@cli.command()
@click.argument("name", required=False)
@click.pass_obj
def resume(ctx, name):
    """Resume from the last exit snapshot, without recopying local directories."""
    with redirect_stdout(sys.stderr if ctx.json else sys.stdout):
        value = Service().up(Config(name=ctx.name(name)), resume_only=True)
    ctx.emit(value)


@cli.command()
@click.argument("name", required=False)
@click.pass_obj
def pause(ctx, name):
    """Stop compute and wait for its filesystem snapshot (up to five minutes)."""
    ctx.emit(Service().pause(ctx.name(name)))


@cli.command("ls")
@click.pass_obj
def list_boxes(ctx):
    """List environments in the active Modal account and environment."""
    ctx.emit(Service().list())


@cli.command()
@click.argument("name", required=False)
@click.pass_obj
def status(ctx, name):
    """Show current status, sandbox ID and connection details."""
    ctx.emit(Service().status(ctx.name(name)))


@cli.command()
@click.argument("name", required=False)
@click.option(
    "--print", "print_only", is_flag=True, help="Print the SSH command without connecting."
)
@click.pass_obj
def ssh(ctx, name, print_only):
    """Open an interactive SSH shell in the configured working directory."""
    record, _ = Service().running(ctx.name(name))
    if print_only:
        import shlex

        command = shlex.join(ssh_argv(record))
        if ctx.json:
            ctx.emit({"ssh_command": command})
        else:
            click.echo(command)
        return
    if not sys.stdin.isatty():
        raise ValueError("Interactive SSH needs a terminal. Use box exec or box ssh --print")
    raise SystemExit(subprocess.call(ssh_argv(record)))


@cli.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_obj
def execute(ctx, name, command):
    """Run COMMAND in NAME; preserve its exit code. Use -- before COMMAND.

    box exec dev -- python --version
    box --json exec dev -- bash -lc 'printf hello'
    box --human exec dev -- npm test  (stream stdout/stderr)
    """
    value = Service().execute(ctx.name(name), command, capture=ctx.json)
    if ctx.json:
        ctx.emit(value)
    raise SystemExit(value["exit_code"])


@cli.command("clone")
@click.argument("name")
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("destination", required=False)
@click.option("--exclude", multiple=True, help="Additional exclusion pattern.")
@click.option(
    "--overwrite", is_flag=True, help="Merge into existing directory, replacing matching files."
)
@click.pass_obj
def clone(ctx, name, source, destination, exclude, overwrite):
    """Copy a local directory now. Defaults to /workspace/<source-name>."""
    from box.transfer import copy_to

    service = Service()
    with service.registry.lock(ctx.name(name)):
        record, sb = service.running(name)
        destination = remote_path(
            destination or f"{record['config']['workdir']}/{source.resolve().name}"
        )
        ctx.emit(
            copy_to(
                sb,
                source.resolve(),
                destination,
                exclude=exclude,
                overwrite=overwrite,
                mounts=tuple(record["config"]["volumes"]),
            )
        )


@cli.command()
@click.argument("name")
@click.argument("source")
@click.argument("destination", type=click.Path(path_type=Path))
@click.option(
    "--overwrite", is_flag=True, help="Merge into existing directory, replacing matching files."
)
@click.pass_obj
def pull(ctx, name, source, destination, overwrite):
    """Copy a remote directory back to this machine."""
    from box.transfer import copy_from

    service = Service()
    with service.registry.lock(ctx.name(name)):
        record, sb = service.running(name)
        ctx.emit(
            copy_from(
                sb,
                remote_path(source),
                destination,
                overwrite=overwrite,
                mounts=tuple(record["config"]["volumes"]),
            )
        )


@cli.command("rm")
@click.argument("name")
@click.option(
    "--yes", is_flag=True, help="Confirm removing the environment record; volumes are retained."
)
@click.pass_obj
def remove(ctx, name, yes):
    """Stop NAME and forget its resume record. Named volumes are retained."""
    if not yes:
        if not sys.stdin.isatty():
            raise ValueError("Removal needs --yes in non-interactive use")
        click.confirm(f"Stop {name} and remove its resume record?", abort=True)
    ctx.emit(Service().remove(ctx.name(name)))


@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="Confirm the previous operation is no longer running.")
@click.pass_obj
def unlock(ctx, name, force):
    """Recover a lock left by a crashed CLI. Never use while an operation is running."""
    if not force:
        raise ValueError("First verify the previous CLI has stopped, then use --force")
    owner = Service().registry.unlock(ctx.name(name))
    ctx.emit({"name": name, "unlocked": owner})


@cli.command()
@click.pass_obj
def doctor(ctx):
    """Check Modal authentication and the local Tailscale connection."""
    checks = {}
    try:
        modal.Client.from_env()
        checks["modal"] = "authenticated"
    except Exception:
        checks["modal"] = "Authentication failed. Run: uvx modal token new"
    try:
        p = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=15
        )
        data = json.loads(p.stdout)
        checks["tailscale"] = data.get("BackendState", "unknown")
    except OSError, ValueError, subprocess.TimeoutExpired:
        checks["tailscale"] = "Unavailable. Install Tailscale and sign into your tailnet"
    checks["tailscale_secret"] = ctx.config().tailscale_secret
    checks["auth_key_in_environment"] = bool(os.environ.get("TAILSCALE_AUTH_KEY"))
    ctx.emit(checks)
    if checks["modal"] != "authenticated" or checks["tailscale"] != "Running":
        raise SystemExit(1)


def main():
    context = None
    try:
        # Modal output is enabled during sandbox creation; --help stays offline.
        context = cli.make_context("box", sys.argv[1:])
        with context:
            cli.invoke(context)
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except Exception as exc:
        error_context = context or getattr(exc, "ctx", None)
        mode = error_context.find_root().params.get("as_json") if error_context else None
        as_json = mode if mode is not None else not sys.stdout.isatty()
        # SDK exceptions can include server diagnostics; never echo credential values.
        message = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
        if isinstance(exc, click.Abort):
            message = "Aborted"
        key = os.environ.get("TAILSCALE_AUTH_KEY")
        if key:
            message = message.replace(key, "[redacted]")
        if as_json:
            click.echo(json.dumps({"error": {"type": type(exc).__name__, "message": message}}))
        else:
            with redirect_stdout(sys.stderr):
                click.echo(f"Error: {message}")
        raise SystemExit(getattr(exc, "exit_code", 1)) from None
