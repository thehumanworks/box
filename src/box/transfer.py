"""Directory transfers using streamed, private tar archives.

Overwrite merges directories, replaces individual files atomically, and leaves
unrelated destination entries intact. An I/O failure during a merge may leave a
partial merge. Symlinks must be relative and remain inside the copied tree.
"""

import errno
import fnmatch
import inspect
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

DEFAULT_EXCLUDES = (".venv", ".direnv", "node_modules", "__pycache__")


def _remote_path(value):
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or "\0" in value
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
    ):
        raise ValueError(f"Path must be absolute, normalized, and not filesystem root: {value!r}")
    return value


def _mounted_path(value, mounts):
    """Resolve a configured Modal volume mount, never arbitrary path links."""
    _remote_path(value)
    matches = [
        mount for mount in mounts if value == _remote_path(mount) or value.startswith(mount + "/")
    ]
    path = Path(value)
    if matches:
        mount = max(matches, key=len)
        path = Path(mount).resolve(strict=True) / value[len(mount) :].lstrip("/")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"Remote path parent is not a configured volume mount: {parent}")
    return path


def _safe_member(member, root):
    parts = PurePosixPath(member.name).parts
    if member.name.startswith("/") or ".." in parts:
        raise ValueError(f"Unsafe archive path: {member.name!r}")
    if not (member.isdir() or member.isfile() or member.issym()):
        raise ValueError(f"Unsupported archive entry (including hard links): {member.name!r}")
    if not parts and not member.isdir():
        raise ValueError("Archive root must be a directory")
    for index in range(1, len(parts)):
        parent = Path(root).joinpath(*parts[:index])
        if parent.is_symlink():
            raise ValueError(f"Archive entry traverses a symlink: {member.name!r}")
    return tarfile.data_filter(member, str(root))


def _git_ignored(source):
    """Ask Git for ignored, untracked entries, relative to the copied directory."""
    env = {**os.environ, "LC_ALL": "C"}
    try:
        probe = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            env=env,
        )
        if probe.returncode:
            if b"not a git repository" in probe.stderr:
                return set()
            raise ValueError(f"Cannot inspect Git repository: {os.fsdecode(probe.stderr).strip()}")
        if probe.stdout.strip() != b"true":
            return set()
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            capture_output=True,
            env=env,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git is required; install git or use --not-respect-ignores") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"Cannot read Git ignore rules: {os.fsdecode(exc.stderr).strip()}"
        ) from exc
    return {os.fsdecode(name).rstrip("/") for name in result.stdout.split(b"\0") if name}


def _pack(source, archive, exclude, ignored=()):
    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Source must be a real directory: {source}")
    count = 0
    with tarfile.open(archive, "w", dereference=False) as output:

        def add(path, relative):
            nonlocal count
            if relative in ignored:
                return
            if relative != "." and any(
                fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(path.name, pattern)
                for pattern in exclude
            ):
                return
            # Copy hard-linked files as independent regular files.
            output.inodes.clear()
            member = output.gettarinfo(str(path), arcname=relative)
            # Conventional Nix build outputs refer to this machine's store,
            # not portable project content. Keep real files/directories and
            # internal links with these names.
            if (
                member.issym()
                and (path.name == "result" or path.name.startswith("result-"))
                and PurePosixPath(member.linkname).is_relative_to("/nix/store")
                and ".." not in PurePosixPath(member.linkname).parts
            ):
                return
            _safe_member(member, source)
            if member.isfile():
                # O_NOFOLLOW also catches a source replaced by a symlink while packing.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as content:
                    current = os.fstat(content.fileno())
                    if not stat.S_ISREG(current.st_mode) or current.st_size != member.size:
                        raise ValueError(f"Source changed while copying: {path}")
                    output.addfile(member, content)
            else:
                output.addfile(member)
            count += 1
            if member.isdir():
                for child in sorted(path.iterdir()):
                    add(child, child.name if relative == "." else relative + "/" + child.name)

        add(source, ".")
    return count


def _destination(path, overwrite, owned_stage=None):
    path = Path(path)
    if path == Path(path.anchor):
        raise ValueError("Destination must not be filesystem root")
    if path.is_symlink():
        raise ValueError(f"Destination must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"Destination is not a directory: {path}")
        if not overwrite and any(entry != owned_stage for entry in path.iterdir()):
            raise FileExistsError(f"Destination is not empty: {path}; use --overwrite to merge")
    # Never let an existing destination parent redirect writes through a link.
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"Destination parent must not be a symlink: {parent}")
    return path


def _publish(source, destination):
    try:
        os.replace(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # Nested volume mounts can cross devices even after root staging.
        # Copy privately on the target filesystem before replacing its entry.
        with tempfile.TemporaryDirectory(prefix=".box-publish-", dir=destination.parent) as local:
            staged = Path(local) / "entry"
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, staged, symlinks=True)
            else:
                shutil.copy2(source, staged, follow_symlinks=False)
            os.replace(staged, destination)


def _merge(source, destination):
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(f"Cannot merge directory into a file or symlink: {target}")
            if target.exists():
                _merge(entry, target)
            else:
                _publish(entry, target)
        else:
            if target.is_dir() and not target.is_symlink():
                raise ValueError(f"Cannot replace directory with a file: {target}")
            _publish(entry, target)
    shutil.copystat(source, destination, follow_symlinks=False)


def _extract(archive, destination, overwrite):
    destination = _destination(destination, overwrite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Existing destinations may be mount roots. Stage on their filesystem so
    # publishing individual entries cannot cross the mount boundary.
    stage_parent = destination if destination.exists() else destination.parent
    with tempfile.TemporaryDirectory(prefix=".box-extract-", dir=stage_parent) as temporary:
        stage = Path(temporary) / "tree"
        stage.mkdir()
        count = 0
        seen = set()
        with tarfile.open(archive, "r|*") as incoming:
            for member in incoming:
                normalized = str(PurePosixPath(member.name))
                if normalized in seen:
                    raise ValueError(f"Duplicate archive entry: {member.name!r}")
                if PurePosixPath(member.name).parts[:1] == (Path(temporary).name,):
                    raise ValueError("Archive entry conflicts with owned staging directory")
                seen.add(normalized)
                incoming.extract(member, stage, filter=_safe_member)
                count += 1
        # Recheck after staging before publishing or merging.
        _destination(destination, overwrite, owned_stage=Path(temporary))
        if destination.exists():
            _merge(stage, destination)
        else:
            stage.rename(destination)
    return count


def _remote(sb, operation, *arguments):
    """Execute only our stdlib archive helpers, passing paths as argv data."""
    helpers = "\n\n".join(
        inspect.getsource(fn)
        for fn in (
            _remote_path,
            _mounted_path,
            _safe_member,
            _pack,
            _destination,
            _publish,
            _merge,
            _extract,
        )
    )
    code = (
        "import errno, fnmatch, json, os, shutil, stat, sys, tarfile, tempfile\n"
        "from pathlib import Path, PurePosixPath\n" + helpers
    )
    code += "\noperation, args = sys.argv[1], json.loads(sys.argv[2])\n"
    code += (
        "if operation == 'pack':\n"
        "    args[0] = _mounted_path(args[0], args.pop())\n"
        "    result = _pack(*args)\n"
        "elif operation == 'extract':\n"
        "    args[1] = _mounted_path(args[1], args.pop())\n"
        "    result = _extract(*args)\n"
        "elif operation == 'temporary': result = tempfile.mkdtemp(prefix='box-transfer-')\n"
        "else: raise ValueError(operation)\n"
        "print(json.dumps(result))\n"
    )
    process = sb.exec("python3", "-c", code, operation, json.dumps(arguments))
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(
            f"Sandbox transfer {operation} failed (exit {returncode}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return json.loads(stdout)


def _cleanup(sb, temporary, failure):
    try:
        sb.filesystem.remove(temporary, recursive=True)
    except Exception as cleanup_error:
        if failure is None:
            raise RuntimeError(
                "Transfer finished but could not remove owned temporary directory "
                f"{temporary}: {cleanup_error}"
            ) from cleanup_error
        failure.add_note(
            f"Also failed to remove owned sandbox temporary directory {temporary}: {cleanup_error}"
        )


def copy_to(
    sb,
    source: Path,
    destination: str,
    *,
    exclude: tuple[str, ...] = (),
    respect_ignores: bool = True,
    overwrite: bool = False,
    mounts: tuple[str, ...] = (),
) -> dict:
    """Copy a directory's contents to an absolute sandbox directory."""
    _remote_path(destination)
    source = Path(source).expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="box-transfer-") as local:
        archive = Path(local) / "archive.tar"
        ignored = _git_ignored(source) if respect_ignores else set()
        _pack(source, archive, DEFAULT_EXCLUDES + tuple(exclude), ignored)
        temporary = _remote(sb, "temporary")
        failure = None
        try:
            remote_archive = temporary + "/archive.tar"
            sb.filesystem.copy_from_local(archive, remote_archive)
            count = _remote(sb, "extract", remote_archive, destination, overwrite, mounts)
            return {
                "source": str(source),
                "destination": destination,
                "entries": count,
                "bytes": archive.stat().st_size,
            }
        except BaseException as error:
            failure = error
            raise
        finally:
            _cleanup(sb, temporary, failure)


def copy_from(
    sb, source: str, destination: Path, *, overwrite: bool = False, mounts: tuple[str, ...] = ()
) -> dict:
    """Copy a sandbox directory, excluding standard dependency/cache folders."""
    _remote_path(source)
    destination = Path(destination).expanduser().absolute()
    # Resolve the parent for platform aliases such as macOS /var -> /private/var,
    # while retaining and rejecting a symlink at the destination itself.
    destination = destination.parent.resolve() / destination.name
    _destination(destination, overwrite)
    with tempfile.TemporaryDirectory(prefix="box-transfer-") as local:
        archive = Path(local) / "archive.tar"
        temporary = _remote(sb, "temporary")
        failure = None
        try:
            remote_archive = temporary + "/archive.tar"
            _remote(sb, "pack", source, remote_archive, DEFAULT_EXCLUDES, mounts)
            sb.filesystem.copy_to_local(remote_archive, archive)
            count = _extract(archive, destination, overwrite)
            return {
                "source": source,
                "destination": str(destination),
                "entries": count,
                "bytes": archive.stat().st_size,
            }
        except BaseException as error:
            failure = error
            raise
        finally:
            _cleanup(sb, temporary, failure)
