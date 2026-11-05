"""Stateful provider doubles exercise lifecycle durability without billable resources."""

import copy
import io
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from box import environment, service, transfer
from box.config import Config
from box.state import Registry


class MemoryDict:
    """Model Modal Dict's serialized values and atomic conditional insertion."""

    def __init__(self):
        self.values = {}
        self.writes = []
        self.mutex = threading.Lock()

    def get(self, key):
        return copy.deepcopy(self.values.get(key))

    def __setitem__(self, key, value):
        self.values[key] = copy.deepcopy(value)
        self.writes.append((key, copy.deepcopy(value)))

    def put(self, key, value, *, skip_if_exists):
        with self.mutex:
            if skip_if_exists and key in self.values:
                return False
            self[key] = value
            return True

    def pop(self, key, *default):
        return self.values.pop(key, *default)

    def items(self):
        return copy.deepcopy(self.values).items()


class Process:
    def __init__(self, code=0, stdout="", stderr=""):
        self.code = code
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def wait(self):
        return self.code


class Sandbox:
    def __init__(self, provider, identifier, image):
        self.provider = provider
        self.object_id = identifier
        self.files = copy.deepcopy(image.files)
        self.code = None
        self.exec_calls = []
        self.snapshot_error = None
        self.snapshot_requests = []
        self.terminate_calls = []
        self.command_result = Process()

    def poll(self):
        return self.code

    def exec(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        return self.command_result

    def terminate(self, *, wait):
        self.terminate_calls.append(wait)
        self.code = 0

    def _experimental_get_exit_snapshot(self, *, timeout):
        self.snapshot_requests.append(timeout)
        assert self.code is not None, "Snapshots require a stopped sandbox"
        if self.snapshot_error:
            raise self.snapshot_error
        snapshot = SimpleNamespace(
            object_id=f"im-exit-{self.object_id}", files=copy.deepcopy(self.files)
        )
        self.provider.images[snapshot.object_id] = snapshot
        return snapshot


class Provider:
    def __init__(self):
        self.sandboxes = {}
        self.images = {}
        self.creations = []
        self.builds = 0
        self.create_error = None
        self.base = SimpleNamespace(object_id="im-base", files={"/base": "installed tools"})

    def build_image(self):
        self.builds += 1
        return self.base

    def from_id(self, identifier):
        if identifier not in self.sandboxes:
            raise service.modal.exception.NotFoundError("Sandbox no longer exists")
        return self.sandboxes[identifier]

    def create(self, *args, **kwargs):
        if self.create_error:
            raise self.create_error
        sb = Sandbox(self, f"sb-{len(self.sandboxes) + 1}", kwargs["image"])
        self.sandboxes[sb.object_id] = sb
        self.creations.append((args, kwargs))
        return sb


@pytest.fixture
def lifecycle(monkeypatch):
    provider = Provider()
    data = MemoryDict()
    registry = Registry(data)
    manager = service.Service(registry)
    monkeypatch.setattr(service.modal.Sandbox, "create", provider.create)
    monkeypatch.setattr(service.modal.Sandbox, "from_id", provider.from_id)
    monkeypatch.setattr(service.modal.Image, "from_id", provider.images.__getitem__)
    monkeypatch.setattr(service.modal.App, "lookup", lambda *a, **k: object())
    monkeypatch.setattr(service.modal.Secret, "from_name", lambda name: ("secret", name))
    monkeypatch.setattr(environment, "build_image", provider.build_image)
    monkeypatch.setattr(service, "git_identity", lambda: {})
    monkeypatch.setattr(
        environment,
        "start_network",
        lambda sb, hostname, **kwargs: {"host": "100.64.0.2", "hostname": hostname},
    )
    return SimpleNamespace(provider=provider, data=data, registry=registry, service=manager)


def test_create_and_timeout_resume_preserve_files_and_enable_exit_snapshots(lifecycle):
    state = lifecycle
    created = state.service.up(Config(name="dev", timeout=123))
    old = state.provider.sandboxes[created["sandbox_id"]]
    old.files["/workspace/unsaved.py"] = "work done after creation"
    old.code = 124  # Provider timeout, without calling box pause.

    resumed = state.service.up(Config(name="dev"), resume_only=True)
    new = state.provider.sandboxes[resumed["sandbox_id"]]

    assert new.files["/workspace/unsaved.py"] == "work done after creation"
    assert new.object_id != old.object_id
    assert state.provider.builds == 1
    assert not old.terminate_calls
    assert old.snapshot_requests == [300]
    assert all(
        kwargs["experimental_options"]["enable_exit_snapshot"] is True
        for _, kwargs in state.provider.creations
    )
    assert state.provider.creations[1][1]["timeout"] == 123
    assert resumed["hostname"] == created["hostname"]


def test_default_1password_secret_is_attached_on_creation_and_restored_on_resume(lifecycle):
    lifecycle.service.up(Config())
    assert lifecycle.registry.get("dev")["config"]["secrets"] == ["1password"]
    lifecycle.service.pause("dev")
    # A caller's current configuration must not erase saved runtime credentials.
    lifecycle.service.up(Config(secrets=[]), resume_only=True)
    assert [kwargs["secrets"] for _, kwargs in lifecycle.provider.creations] == [
        [("secret", "1password")],
        [("secret", "1password")],
    ]


def test_pause_then_resume_uses_saved_snapshot(lifecycle):
    created = lifecycle.service.up(Config())
    old = lifecycle.provider.sandboxes[created["sandbox_id"]]
    old.files["/workspace/file"] = "retained"
    paused = lifecycle.service.pause("dev")
    assert paused["snapshot_id"] in lifecycle.provider.images
    resumed = lifecycle.service.up(Config(), resume_only=True)
    restored = lifecycle.provider.sandboxes[resumed["sandbox_id"]]
    assert restored.files["/workspace/file"] == "retained"
    assert old.snapshot_requests == [300]


@pytest.mark.parametrize("saved_snapshot", [False, True])
def test_missing_snapshot_never_creates_fresh_replacement(lifecycle, saved_snapshot):
    created = lifecycle.service.up(Config())
    old = lifecycle.provider.sandboxes[created["sandbox_id"]]
    if saved_snapshot:
        paused = lifecycle.service.pause("dev")
        del lifecycle.provider.images[paused["snapshot_id"]]
    else:
        old.code = 124
        old.snapshot_error = RuntimeError("expired snapshot")
    before = lifecycle.registry.get("dev")
    with pytest.raises(ValueError, match="No replacement was created"):
        lifecycle.service.up(Config(), resume_only=True)
    assert lifecycle.registry.get("dev") == before
    assert len(lifecycle.provider.creations) == 1
    assert lifecycle.provider.builds == 1
    assert "lock:dev" not in lifecycle.data.values


def test_resume_does_not_require_or_recopy_original_local_clone(lifecycle, monkeypatch, tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    copies = []

    def copy_to(sb, path, destination, **kwargs):
        copies.append((path, destination))
        sb.files[destination + "/file"] = "original"

    monkeypatch.setattr(transfer, "copy_to", copy_to)
    config = Config(clones=[{"source": str(source), "destination": "/workspace/repo"}])
    first = lifecycle.service.up(config)
    sb = lifecycle.provider.sandboxes[first["sandbox_id"]]
    sb.files["/workspace/repo/file"] = "edited remotely"
    source.rmdir()
    lifecycle.service.pause("dev")
    resumed = lifecycle.service.up(Config(), resume_only=True)
    new = lifecycle.provider.sandboxes[resumed["sandbox_id"]]
    assert new.files["/workspace/repo/file"] == "edited remotely"
    assert copies == [(source, "/workspace/repo")]
    assert resumed["cloned"] == ["/workspace/repo"]


def test_reconnect_running_sandbox_does_not_create_or_snapshot(lifecycle):
    first = lifecycle.service.up(Config())
    second = lifecycle.service.up(Config())
    assert second["sandbox_id"] == first["sandbox_id"]
    assert len(lifecycle.provider.creations) == 1
    sb = lifecycle.provider.sandboxes[first["sandbox_id"]]
    assert not sb.snapshot_requests
    assert sb.exec_calls == [
        (("mkdir", "-p", "--", "/workspace"), {"workdir": "/"}),
        (("mkdir", "-p", "--", "/workspace"), {"workdir": "/"}),
    ]


def test_setup_failure_persists_handle_before_network_and_releases_lock(lifecycle, monkeypatch):
    def fail_network(sb, *args, **kwargs):
        assert lifecycle.registry.get("dev")["sandbox_id"] == sb.object_id
        assert lifecycle.registry.get("dev")["status"] == "starting"
        assert "lock:dev" in lifecycle.data.values
        sb.files["/workspace/partial"] = "recover me"
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(environment, "start_network", fail_network)
    with pytest.raises(RuntimeError, match="network unavailable"):
        lifecycle.service.up(Config())
    record = lifecycle.registry.get("dev")
    assert record["status"] == "setup-failed"
    sb = lifecycle.provider.sandboxes[record["sandbox_id"]]
    assert sb.terminate_calls == [True]
    assert sb.files["/workspace/partial"] == "recover me"
    assert "lock:dev" not in lifecycle.data.values


def test_failed_resume_keeps_previous_snapshot_and_latest_partial_files(lifecycle, monkeypatch):
    original = lifecycle.service.up(Config())
    paused = lifecycle.service.pause("dev")

    def fail_network(sb, *args, **kwargs):
        sb.files["/workspace/latest"] = "changes from interrupted setup"
        raise RuntimeError("setup interrupted")

    monkeypatch.setattr(environment, "start_network", fail_network)
    with pytest.raises(RuntimeError, match="setup interrupted"):
        lifecycle.service.up(Config(), resume_only=True)
    failed = lifecycle.registry.get("dev")
    assert failed["previous_snapshot_id"] == paused["snapshot_id"]
    assert failed["previous_sandbox_id"] == original["sandbox_id"]
    assert failed["sandbox_id"] != original["sandbox_id"]
    monkeypatch.setattr(environment, "start_network", lambda *a, **k: {"host": "100.64.0.2"})
    resumed = lifecycle.service.up(Config(), resume_only=True)
    assert lifecycle.provider.sandboxes[resumed["sandbox_id"]].files["/workspace/latest"] == (
        "changes from interrupted setup"
    )


def test_provider_creation_failure_leaves_old_snapshot_usable(lifecycle):
    lifecycle.service.up(Config())
    lifecycle.service.pause("dev")
    before = lifecycle.registry.get("dev")
    lifecycle.provider.create_error = RuntimeError("quota exceeded")
    with pytest.raises(RuntimeError, match="quota exceeded"):
        lifecycle.service.up(Config(), resume_only=True)
    assert lifecycle.registry.get("dev") == before
    assert "lock:dev" not in lifecycle.data.values


def test_pause_snapshot_failure_retains_stopped_sandbox_for_retry(lifecycle):
    created = lifecycle.service.up(Config())
    sb = lifecycle.provider.sandboxes[created["sandbox_id"]]
    sb.snapshot_error = RuntimeError("snapshot pending")
    with pytest.raises(ValueError, match="sandbox ID is retained"):
        lifecycle.service.pause("dev")
    assert lifecycle.registry.get("dev")["sandbox_id"] == sb.object_id
    assert lifecycle.registry.get("dev")["status"] == "paused"
    sb.snapshot_error = None
    assert lifecycle.service.up(Config(), resume_only=True)["status"] == "running"


def test_unknown_resume_does_not_build_or_create(lifecycle):
    with pytest.raises(ValueError, match="nothing to resume"):
        lifecycle.service.up(Config(), resume_only=True)
    assert lifecycle.provider.builds == 0
    assert not lifecycle.provider.creations


def test_missing_sandbox_is_unavailable_without_breaking_other_list_entries(lifecycle):
    expired = lifecycle.service.up(Config(name="expired"))
    lifecycle.service.up(Config(name="active"))
    del lifecycle.provider.sandboxes[expired["sandbox_id"]]

    rows = {row["name"]: row for row in lifecycle.service.list()}
    assert rows["active"]["status"] == "running"
    assert rows["expired"]["status"] == "unavailable"
    assert rows["expired"]["sandbox_id"] == expired["sandbox_id"]
    assert lifecycle.registry.get("expired") is not None


def test_remove_can_forget_an_already_deleted_provider_sandbox(lifecycle):
    created = lifecycle.service.up(Config())
    record = lifecycle.registry.get("dev")
    record["config"]["volumes"] = {"/mnt/data": "retained-data"}
    lifecycle.registry.save(record)
    del lifecycle.provider.sandboxes[created["sandbox_id"]]

    removed = lifecycle.service.remove("dev")
    assert removed["status"] == "removed"
    assert removed["retained_volumes"] == {"/mnt/data": "retained-data"}
    assert lifecycle.registry.get("dev") is None
    assert "lock:dev" not in lifecycle.data.values


def test_invalid_clone_fails_before_billable_creation(lifecycle, tmp_path):
    cfg = Config(clones=[{"source": str(tmp_path / "missing"), "destination": "/workspace/repo"}])
    with pytest.raises(ValueError, match="not a directory"):
        lifecycle.service.up(cfg)
    assert not lifecycle.provider.creations
    assert not lifecycle.registry.get("dev")


def test_conflicting_operations_are_blocked_without_provider_changes(lifecycle):
    lifecycle.service.up(Config())
    with lifecycle.registry.lock("dev"):
        owner = lifecycle.data.get("lock:dev")
        for operation in (
            lambda: lifecycle.service.up(Config()),
            lambda: lifecycle.service.pause("dev"),
            lambda: lifecycle.service.remove("dev"),
        ):
            with pytest.raises(ValueError, match="Another operation holds"):
                operation()
            assert lifecycle.data.get("lock:dev") == owner
    assert len(lifecycle.provider.creations) == 1
    assert lifecycle.registry.get("dev")["status"] == "running"


def test_registry_lock_excludes_another_client_and_releases_after_exception():
    data = MemoryDict()
    first, second = Registry(data), Registry(data)
    with pytest.raises(RuntimeError, match="failed operation"), first.lock("dev"):
        with ThreadPoolExecutor(max_workers=1) as pool:

            def attempt():
                with second.lock("dev"):
                    pytest.fail("second client acquired held lock")

            with pytest.raises(ValueError, match="Another operation holds"):
                pool.submit(attempt).result()
        with second.lock("other"):
            pass
        raise RuntimeError("failed operation")
    with second.lock("dev"):
        assert data.get("lock:dev")
    assert not data.values


def test_execute_preserves_argv_and_remote_exit_output(lifecycle):
    created = lifecycle.service.up(Config(workdir="/workspace/a b"))
    sb = lifecycle.provider.sandboxes[created["sandbox_id"]]
    sb.command_result = Process(23, "out\n", "err\n")
    argv = ("python", "-c", "print('hello')", "$(touch /tmp/no)", ";", "", "a b")
    value = lifecycle.service.execute("dev", argv, capture=True, timeout=42)
    assert value == {"exit_code": 23, "stdout": "out\n", "stderr": "err\n"}
    invoked, options = sb.exec_calls[-1]
    assert invoked[:2] == ("/bin/bash", "-c")
    assert invoked[4:] == argv
    assert "mkdir" not in invoked[2]
    assert options == {"workdir": "/workspace/a b", "timeout": 42}


def test_inherited_git_identity_targets_writable_file(lifecycle, monkeypatch):
    identity = {"user.name": "Test $(author)", "user.email": "author@example.test"}
    monkeypatch.setattr(service, "git_identity", lambda: identity)
    created = lifecycle.service.up(Config())
    calls = lifecycle.provider.sandboxes[created["sandbox_id"]].exec_calls
    for key, value in identity.items():
        assert (
            ("git", "config", "--file", "/root/.gitconfig", key, value),
            {"workdir": "/"},
        ) in calls


def test_ssh_working_directory_is_quoted_as_one_shell_argument():
    path = "/workspace/a b; $(touch /tmp/no) ' quoted"
    args = service.ssh_argv({"host": "100.64.0.2", "config": {"workdir": path}})
    assert args[-2] == "root@100.64.0.2"
    assert shlex.split(args[-1]) == ["cd", "--", path, "&&", "exec", "bash", "-l"]


def interrupted_box(state, config=None):
    """A process died after saving the provider handle, before setup completed."""
    config = (config or Config()).validate()
    sb = state.provider.create(image=state.provider.base)
    state.registry.save(
        {
            "name": config.name,
            "config": config.to_dict(),
            "sandbox_id": sb.object_id,
            "hostname": "box-dev-test",
            "status": "starting",
            "cloned": [],
        }
    )
    return sb


def test_live_interrupted_box_is_not_ready_and_cwd_allows_only_explicit_inspection(
    lifecycle, capsys
):
    sb = interrupted_box(lifecycle)
    before = lifecycle.registry.get("dev")
    status = lifecycle.service.status("dev")
    assert status["status"] == "setup-incomplete"
    assert status["setup"]["complete"] is False
    with pytest.raises(ValueError, match=r"setup is incomplete.*box up dev.*--cwd /"):
        lifecycle.service.execute("dev", ("pwd",), capture=True)
    assert sb.exec_calls == []
    lifecycle.service.execute("dev", ("pwd",), capture=True, cwd="/")
    assert sb.exec_calls[-1][1]["workdir"] == "/"
    assert "mkdir" not in sb.exec_calls[-1][0][2]
    assert "setup is incomplete" in capsys.readouterr().err
    # Neither status nor diagnostic exec can promote or mutate the record.
    assert lifecycle.registry.get("dev") == before
    ready = lifecycle.service.up(Config())
    assert ready["status"] == "running"
    assert ready["setup"]["complete"] is True
    assert ready["sandbox_id"] == sb.object_id
    assert len(lifecycle.provider.creations) == 1
    assert not sb.terminate_calls
    lifecycle.service.execute("dev", ("pwd",), capture=True)
    assert sb.exec_calls[-1][1]["workdir"] == "/workspace"


@pytest.mark.parametrize("stopped", [False, True])
def test_recovery_completes_pending_clones_without_requiring_completed_sources(
    lifecycle, monkeypatch, tmp_path, stopped
):
    pending = tmp_path / "pending"
    pending.mkdir()
    config = Config(
        clones=[
            {"source": str(tmp_path / "deleted-source"), "destination": "/workspace/done"},
            {"source": str(pending), "destination": "/workspace/pending"},
        ]
    )
    old = interrupted_box(lifecycle, config)
    old.files["/workspace/done/edited"] = "keep user edits"
    record = lifecycle.registry.get("dev")
    record["cloned"] = ["/workspace/done"]
    lifecycle.registry.save(record)
    if stopped:
        old.code = 124
    copies = []

    def copy(sb, source, destination, **kwargs):
        copies.append(destination)
        assert not kwargs.get("overwrite", False)
        sb.files[destination + "/copied"] = "copied"

    monkeypatch.setattr(transfer, "copy_to", copy)
    ready = lifecycle.service.up(Config(), resume_only=True)
    sb = lifecycle.provider.sandboxes[ready["sandbox_id"]]
    assert copies == ["/workspace/pending"]
    assert sb.files["/workspace/done/edited"] == "keep user edits"
    assert ready["cloned"] == ["/workspace/done", "/workspace/pending"]
    assert ready["setup"]["complete"]
    assert bool(sb is not old) == stopped


@pytest.mark.parametrize("stopped", [False, True])
def test_missing_pending_source_keeps_live_or_restored_box_and_reports_phase(
    lifecycle, tmp_path, stopped
):
    sb = interrupted_box(
        lifecycle,
        Config(
            clones=[
                {"source": str(tmp_path / "absent"), "destination": "/workspace/pending"},
            ]
        ),
    )
    sb.files["/workspace/user-work"] = "never lose this"
    if stopped:
        sb.code = 0
    with pytest.raises(ValueError, match="source is unavailable.*original client"):
        lifecycle.service.up(Config())
    record = lifecycle.registry.get("dev")
    current = lifecycle.provider.sandboxes[record["sandbox_id"]]
    assert current.poll() is None
    assert not current.terminate_calls
    assert current.files["/workspace/user-work"] == "never lose this"
    assert record["setup"]["phase"] == "clones"
    assert record["setup"]["complete"] is False
    assert lifecycle.service.status("dev")["status"] == "setup-incomplete"
    assert "lock:dev" not in lifecycle.data.values


@pytest.mark.parametrize("phase", ["workdir", "clones", "git", "network"])
@pytest.mark.parametrize("exception_type", [RuntimeError, KeyboardInterrupt])
def test_fresh_creation_checkpoints_each_failed_phase_and_preserves_snapshot(
    lifecycle, monkeypatch, tmp_path, phase, exception_type
):
    source = tmp_path / "source"
    source.mkdir()
    config = Config(clones=[{"source": str(source), "destination": "/workspace/repo"}])
    monkeypatch.setattr(service, "git_identity", lambda: {"user.name": "Original Author"})
    original_exec = Sandbox.exec

    def fail():
        record = lifecycle.registry.get("dev")
        assert record["setup"]["phase"] == phase
        assert record["setup"]["complete"] is False
        assert record["status"] == "starting"
        raise exception_type("injected failure")

    def execute(sb, *args, **kwargs):
        if (phase == "workdir" and args[0] == "mkdir") or (phase == "git" and args[0] == "git"):
            fail()
        return original_exec(sb, *args, **kwargs)

    monkeypatch.setattr(Sandbox, "exec", execute)
    monkeypatch.setattr(transfer, "copy_to", lambda *a, **k: fail() if phase == "clones" else None)
    if phase == "network":
        monkeypatch.setattr(environment, "start_network", lambda *a, **k: fail())
    surfaced = (
        ValueError if phase == "clones" and exception_type is RuntimeError else exception_type
    )
    with pytest.raises(surfaced, match="injected failure"):
        lifecycle.service.up(config)
    record = lifecycle.registry.get("dev")
    sb = lifecycle.provider.sandboxes[record["sandbox_id"]]
    assert record["status"] == "setup-failed"
    assert record["setup"]["phase"] == phase
    assert record["setup"]["complete"] is False
    assert sb.terminate_calls == [True]
    assert record["git_identity"] == {"user.name": "Original Author"}
    assert "lock:dev" not in lifecycle.data.values


def test_completed_but_uncheckpointed_clone_is_not_overwritten_or_assumed_ready(
    lifecycle, monkeypatch, tmp_path
):
    source, destination = tmp_path / "source", tmp_path / "remote-repo"
    source.mkdir()
    (source / "file").write_text("source contents")
    archive = tmp_path / "archive.tar"
    transfer._pack(source, archive, ())
    transfer._extract(archive, destination, False)
    (destination / "file").write_text("remote edits after ambiguous copy")
    sb = interrupted_box(
        lifecycle,
        Config(
            clones=[
                {"source": str(source), "destination": "/workspace/repo"},
            ]
        ),
    )

    def copy(*args, **kwargs):
        assert not kwargs.get("overwrite", False)
        transfer._extract(archive, destination, False)

    monkeypatch.setattr(transfer, "copy_to", copy)
    with pytest.raises(ValueError, match="not confirmed complete.*will not be overwritten"):
        lifecycle.service.up(Config())
    assert (destination / "file").read_text() == "remote edits after ambiguous copy"
    assert lifecycle.registry.get("dev")["cloned"] == []
    assert not sb.terminate_calls
    assert not lifecycle.registry.get("dev")["setup"]["complete"]


def test_git_identity_checkpoint_survives_failure_and_is_not_reapplied_on_resume(
    lifecycle, monkeypatch
):
    identity = {"user.name": "Original Author", "user.email": "original@example.test"}
    monkeypatch.setattr(service, "git_identity", lambda: identity)
    network = environment.start_network
    monkeypatch.setattr(
        environment,
        "start_network",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    with pytest.raises(RuntimeError):
        lifecycle.service.up(Config())
    record = lifecycle.registry.get("dev")
    assert record["setup"]["git_applied"] is True
    assert record["git_identity"] == identity
    monkeypatch.setattr(environment, "start_network", network)
    monkeypatch.setattr(service, "git_identity", lambda: pytest.fail("must use saved identity"))
    ready = lifecycle.service.up(Config(), resume_only=True)
    sb = lifecycle.provider.sandboxes[ready["sandbox_id"]]
    assert not any(args[0] == "git" for args, _ in sb.exec_calls)
    assert "git_identity" not in ready


@pytest.mark.parametrize("legacy_status", ["running", "paused", "starting", "setup-failed"])
@pytest.mark.parametrize("clones_done", [False, True])
def test_legacy_readiness_requires_saved_completion_and_clone_ledger(
    lifecycle, legacy_status, clones_done
):
    interrupted_box(
        lifecycle,
        Config(
            clones=[
                {"source": "/unavailable", "destination": "/workspace/repo"},
            ]
        ),
    )
    record = lifecycle.registry.get("dev")
    record.update(status=legacy_status, host="100.64.0.2")
    if clones_done:
        record["cloned"] = ["/workspace/repo"]
    lifecycle.registry.save(record)
    expected = clones_done and legacy_status in {"running", "paused"}
    assert lifecycle.service.require("dev")["setup"]["complete"] == expected
    assert "setup" not in lifecycle.registry.get("dev")  # no writes during reads


def test_unknown_setup_version_is_not_silently_downgraded(lifecycle):
    interrupted_box(lifecycle)
    record = lifecycle.registry.get("dev")
    record["setup"] = {"version": 999, "complete": True}
    lifecycle.registry.save(record)
    with pytest.raises(ValueError, match="Unsupported box setup version"):
        lifecycle.service.up(Config())
    assert lifecycle.registry.get("dev")["setup"]["version"] == 999
    assert "lock:dev" not in lifecycle.data.values


@pytest.mark.parametrize("capture", [False, True])
def test_exec_missing_cwd_preserves_failure_adds_advice_and_never_retries(
    lifecycle, capture, capsys
):
    created = lifecycle.service.up(Config())
    sb = lifecycle.provider.sandboxes[created["sandbox_id"]]
    sb.exec_calls.clear()
    diagnostic = 'failed to find initial working directory "/missing": no such file or directory\n'
    sb.command_result = Process(128, "", diagnostic)
    before = lifecycle.registry.get("dev")
    response = lifecycle.service.execute("dev", ("false",), cwd="/missing", capture=capture)
    assert response["exit_code"] == 128
    stderr = response["stderr"] if capture else capsys.readouterr().err
    assert diagnostic in stderr
    assert "box exec --cwd / dev" in stderr
    assert "No directory was created" in stderr
    assert len(sb.exec_calls) == 1
    assert sb.exec_calls[0][1]["workdir"] == "/missing"
    assert "mkdir" not in sb.exec_calls[0][0][2]
    assert lifecycle.registry.get("dev") == before


def test_refresh_failure_does_not_erase_completed_setup(lifecycle, monkeypatch):
    created = lifecycle.service.up(Config())
    before = lifecycle.registry.get("dev")
    sb = lifecycle.provider.sandboxes[created["sandbox_id"]]

    def unavailable(*args, **kwargs):
        raise RuntimeError("network refresh failed")

    monkeypatch.setattr(environment, "start_network", unavailable)
    with pytest.raises(RuntimeError, match="network refresh failed"):
        lifecycle.service.up(Config())
    assert lifecycle.registry.get("dev") == before
    assert lifecycle.service.execute("dev", ("true",), capture=True)["exit_code"] == 0
    assert not sb.terminate_calls


@pytest.mark.parametrize("saved_snapshot", [False, True])
def test_resume_missing_provider_handle_uses_only_a_saved_snapshot(lifecycle, saved_snapshot):
    first = lifecycle.service.up(Config())
    if saved_snapshot:
        lifecycle.service.pause("dev")
    before = lifecycle.registry.get("dev")
    del lifecycle.provider.sandboxes[first["sandbox_id"]]
    if saved_snapshot:
        # The test provider derives IDs from count; ensure its new ID differs.
        lifecycle.provider.sandboxes["sb-unused"] = object()
        resumed = lifecycle.service.up(Config(), resume_only=True)
        assert resumed["status"] == "running"
        assert resumed["previous_sandbox_id"] == first["sandbox_id"]
    else:
        with pytest.raises(ValueError, match="No replacement was created"):
            lifecycle.service.up(Config(), resume_only=True)
        assert lifecycle.registry.get("dev") == before
        assert len(lifecycle.provider.creations) == 1


def test_restored_incomplete_setup_does_not_advertise_old_endpoints(lifecycle, monkeypatch):
    lifecycle.service.up(Config())
    lifecycle.service.pause("dev")

    def unavailable(*args, **kwargs):
        raise RuntimeError("network setup failed")

    monkeypatch.setattr(environment, "start_network", unavailable)
    with pytest.raises(RuntimeError):
        lifecycle.service.up(Config(), resume_only=True)
    status = lifecycle.service.status("dev")
    assert status["status"] == "setup-incomplete"
    assert "host" not in status and "ssh_command" not in status
    assert status["setup"]["complete"] is False


def test_legacy_record_without_clone_ledger_recovers(lifecycle):
    interrupted_box(lifecycle)
    record = lifecycle.registry.get("dev")
    del record["cloned"]
    lifecycle.registry.save(record)
    ready = lifecycle.service.up(Config())
    assert ready["status"] == "running"
    assert ready["cloned"] == []


def test_partial_git_application_retries_the_saved_identity(lifecycle, monkeypatch):
    identity = {"user.name": "Original Author", "user.email": "original@example.test"}
    monkeypatch.setattr(service, "git_identity", lambda: identity)
    original = Sandbox.exec
    failed = False

    def fail_once(sb, *args, **kwargs):
        nonlocal failed
        if args[0] == "git" and "user.email" in args and not failed:
            failed = True
            return Process(1)
        return original(sb, *args, **kwargs)

    monkeypatch.setattr(Sandbox, "exec", fail_once)
    with pytest.raises(ValueError, match="Cannot apply Git"):
        lifecycle.service.up(Config())
    before = lifecycle.registry.get("dev")
    assert not before["setup"]["git_applied"]
    monkeypatch.setattr(service, "git_identity", lambda: pytest.fail("must not read new author"))
    ready = lifecycle.service.up(Config(), resume_only=True)
    sb = lifecycle.provider.sandboxes[ready["sandbox_id"]]
    for key, value in identity.items():
        assert (
            ("git", "config", "--file", "/root/.gitconfig", key, value),
            {"workdir": "/"},
        ) in sb.exec_calls
    assert ready["setup"]["git_applied"]


def test_exec_invalid_cwd_fails_before_provider_access(lifecycle, monkeypatch):
    monkeypatch.setattr(
        lifecycle.service, "running", lambda *a, **k: pytest.fail("provider access")
    )
    with pytest.raises(ValueError, match="absolute"):
        lifecycle.service.execute("dev", ("pwd",), cwd="relative")
