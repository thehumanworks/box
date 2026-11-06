"""One interface for people, scripts, and coding agents."""

import json
import math
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import click
import modal

from box import __version__
from box.config import Config, load_config, name_value, remote_path
from box.modal_transport import connect
from box.remote_client import (
    CALL_ID,
    MAX_TIMEOUT,
    REQUEST_ID,
    SYNC_TIMEOUT,
    RemoteClient,
    RemoteClientError,
)
from box.service import Service, ssh_argv


class Context:
    def __init__(self, config_path, as_json, deployment_path=None):
        self.config_path = config_path
        self.deployment_path = deployment_path
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
            if value.get("setup") and not value["setup"]["complete"]:
                click.echo(
                    f"Setup incomplete at {value['setup']['phase']}; "
                    f"run box up {value['name']} to finish."
                )
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
@click.option(
    "--remote-deployment",
    "deployment_path",
    envvar="BOX_REMOTE_DEPLOYMENT",
    type=click.Path(path_type=Path, dir_okay=False),
    show_envvar=True,
    help="HTTPS deployment manifest; auto-detects a single locally provisioned deployment.",
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx, config_path, as_json, deployment_path):
    """Portable development environments with Tailscale SSH.

    Start: box up dev --clone .    Connect: box ssh dev
    Pause: box pause dev          Continue: box resume dev

    Pausing saves files. Processes restart on resume. No prompt is needed for
    normal operations; destructive removal requires --yes in scripts.
    """
    ctx.obj = Context(config_path, as_json, deployment_path)


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
@click.option(
    "--respect-ignores/--not-respect-ignores",
    default=None,
    help="Respect Git ignore rules for initial clones (default: config or enabled).",
)
@click.pass_obj
def up(
    ctx,
    name,
    sources,
    mounts,
    cpu,
    memory,
    timeout,
    idle_timeout,
    tailscale_secret,
    respect_ignores,
):
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
    if respect_ignores is not None:
        for clone in config.clones:
            clone["respect_ignores"] = respect_ignores
    for spec in mounts:
        volume, separator, mount = spec.partition(":")
        if not separator or not volume:
            raise ValueError("--volume expects NAME:/absolute/mount/path")
        config.volumes[remote_path(mount)] = volume
    service = Service()
    if service.registry.get(config.name) and (
        sources
        or mounts
        or respect_ignores is not None
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
    """Resume saved files and finish setup; completed clones are never recopied."""
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


def exec_cwd(ctx, param, value):
    if value is None:
        return None
    try:
        return remote_path(value, allow_root=True)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def request_identity(ctx, param, value):
    if value is not None and not REQUEST_ID.fullmatch(value):
        raise click.BadParameter("Use 8–128 ASCII letters, digits, underscores or hyphens")
    return value


def finite_seconds(ctx, param, value):
    if not math.isfinite(value):
        raise click.BadParameter("Seconds must be finite")
    return value


def call_identity(ctx, param, value):
    if not CALL_ID.fullmatch(value):
        raise click.BadParameter("Expected fc- followed by letters and digits")
    return value


def emit_execution(ctx, value):
    """HTTP success is not command success; JSON stays one untouched document."""
    outcome = value.get("result", value)
    if ctx.json:
        ctx.emit(value)
    else:
        click.echo(outcome.get("stdout", ""), nl=False, color=True)
        click.echo(outcome.get("stderr", ""), nl=False, err=True, color=True)
        if outcome.get("stdout_truncated") or outcome.get("stderr_truncated"):
            click.echo("box: Output truncated at 1 MiB per stream.", err=True)
        if outcome.get("status") == "pending":
            click.echo(f"{value['call_id']}: pending")
        elif outcome.get("status") == "expired":
            click.echo(f"box: {value['call_id']}: result expired", err=True)
        elif "error" in outcome:
            error = outcome["error"]
            click.echo(
                f"box: {error.get('message', error.get('code', 'Remote execution failed'))}",
                err=True,
            )
            for key in ("request_id", "call_id"):
                if outcome.get(key):
                    click.echo(f"box: {key}={outcome[key]}", err=True)
    if outcome.get("status") == "completed":
        return outcome["exit_code"]
    if outcome.get("status") == "pending":
        return 75
    if outcome.get("error", {}).get("code") in {"exec_timeout", "worker_timeout"}:
        return 124
    return 1


@cli.command("exec", context_settings={"ignore_unknown_options": True})
@click.option(
    "--cwd",
    "-w",
    callback=exec_cwd,
    metavar="/PATH",
    help="Existing remote directory for this command only; allows inspecting incomplete setup.",
)
@click.option(
    "--timeout",
    type=click.IntRange(1, SYNC_TIMEOUT),
    default=60,
    show_default=True,
    help="Command timeout in seconds. For longer work use spawn/poll.",
)
@click.option(
    "--request-id",
    callback=request_identity,
    help="Reuse with identical input after an uncertain request; generated otherwise.",
)
@click.argument("name")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_obj
def execute(ctx, name, command, cwd, timeout, request_id):
    """Run COMMAND over HTTPS; preserve its exit code. Use -- before COMMAND.

    box exec dev -- python --version
    box exec --cwd / dev -- ls -la
    box --json exec dev -- bash -lc 'printf hello'
    box --human exec dev -- npm test  (buffered stdout/stderr, not streaming)
    """
    name = ctx.name(name)
    value = RemoteClient(ctx.deployment_path).execute(
        name,
        command,
        cwd=cwd,
        timeout=timeout,
        request_id=request_id,
    )
    raise SystemExit(emit_execution(ctx, value))


@cli.command(context_settings={"ignore_unknown_options": True})
@click.option(
    "--cwd",
    "-w",
    callback=exec_cwd,
    metavar="/PATH",
    help="Existing remote directory for this command only.",
)
@click.option(
    "--timeout",
    type=click.IntRange(1, MAX_TIMEOUT),
    default=MAX_TIMEOUT,
    show_default=True,
    help="Command timeout in seconds.",
)
@click.option(
    "--request-id",
    callback=request_identity,
    help="Reuse with identical input after an uncertain submission; generated otherwise.",
)
@click.argument("name")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_obj
def spawn(ctx, name, command, cwd, timeout, request_id):
    """Start COMMAND in the background. Use box poll CALL_ID to retrieve its result.

    box spawn dev -- python long_job.py
    box --human spawn dev -- make test  (prints only the call ID)
    """
    name = ctx.name(name)
    value = RemoteClient(ctx.deployment_path).spawn(
        name,
        command,
        cwd=cwd,
        timeout=timeout,
        request_id=request_id,
    )
    if value.get("status") != "submitted":
        raise SystemExit(emit_execution(ctx, value))
    if ctx.json:
        ctx.emit(value)
    else:
        click.echo(value["call_id"])


@cli.command()
@click.argument("call_id", callback=call_identity)
@click.option("--wait", is_flag=True, help="Keep polling until a terminal result or wait deadline.")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True, max=86400),
    callback=finite_seconds,
    default=3600,
    show_default=True,
    help="Maximum wait in seconds; does not cancel the job.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0, min_open=True, max=60),
    callback=finite_seconds,
    default=1,
    show_default=True,
    help="Seconds between pending polls with --wait.",
)
@click.pass_obj
def poll(ctx, call_id, wait, timeout, interval):
    """Fetch a call result; exit 75 if pending, or preserve the completed command's exit.

    box poll fc-...                  (one check)
    box poll --wait fc-...           (wait, without submitting another command)
    """
    client = RemoteClient(ctx.deployment_path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            value = client.poll(call_id, timeout=min(30, max(0.001, deadline - time.monotonic())))
            if not wait or value.get("status") != "pending":
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
            if time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        raise RemoteClientError(
            "Polling interrupted; the job was not cancelled. Run box poll " + call_id,
            call_id=call_id,
            exit_code=130,
        ) from None
    raise SystemExit(emit_execution(ctx, value))


@cli.command("clone")
@click.argument("name")
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("destination", required=False)
@click.option("--exclude", multiple=True, help="Additional exclusion pattern.")
@click.option(
    "--respect-ignores/--not-respect-ignores",
    default=True,
    show_default=True,
    help="Respect Git ignore rules, keeping tracked files.",
)
@click.option(
    "--overwrite", is_flag=True, help="Merge into existing directory, replacing matching files."
)
@click.pass_obj
def clone(ctx, name, source, destination, exclude, overwrite, respect_ignores):
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
                respect_ignores=respect_ignores,
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
        connect()
        checks["modal"] = "authenticated"
    except modal.exception.AuthError:
        checks["modal"] = (
            "Authentication failed. Check Modal credentials or run: uvx modal token new"
        )
    except Exception:
        checks["modal"] = (
            "Connection check failed. Check VPN/proxy access and the host CA trust "
            "(SSL_CERT_FILE / SSL_CERT_DIR); this is not necessarily an authentication failure."
        )
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
            value = (
                exc.result()
                if isinstance(exc, RemoteClientError)
                else {"error": {"type": type(exc).__name__, "message": message}}
            )
            value["error"]["message"] = message
            click.echo(json.dumps(value))
        else:
            with redirect_stdout(sys.stderr):
                click.echo(f"Error: {message}")
        raise SystemExit(getattr(exc, "exit_code", 1)) from None
