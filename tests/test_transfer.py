import errno
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from box import transfer


class Process:
    def __init__(self, args):
        completed = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True)
        self.stdout = io.StringIO(completed.stdout)
        self.stderr = io.StringIO(completed.stderr)
        self.returncode = completed.returncode

    def wait(self):
        return self.returncode


class Filesystem:
    def __init__(self):
        self.removed = []
        self.fail_upload = False
        self.fail_download = False
        self.fail_cleanup = False

    def copy_from_local(self, source, target):
        if self.fail_upload:
            raise OSError("upload interrupted")
        shutil.copyfile(source, target)

    def copy_to_local(self, source, target):
        if self.fail_download:
            raise OSError("download interrupted")
        shutil.copyfile(source, target)

    def remove(self, path, recursive=False):
        self.removed.append(path)
        shutil.rmtree(path)
        if self.fail_cleanup:
            raise OSError("cleanup interrupted")


class Sandbox:
    def __init__(self):
        self.filesystem = Filesystem()

    def exec(self, *args):
        return Process(args)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "destination"
        self.sb = Sandbox()

    def populate(self):
        (self.source / ".git").mkdir()
        (self.source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.source / ".env").write_text("EXPLICIT_SOURCE=1\n")
        (self.source / "empty").mkdir()
        script = self.source / "run"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        (self.source / "link").symlink_to("run")
        for name in transfer.DEFAULT_EXCLUDES:
            (self.source / name).mkdir()
            (self.source / name / "cache").write_text("omit")

    def assert_tree(self, path):
        self.assertEqual((path / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertTrue((path / ".env").is_file())
        self.assertTrue((path / "empty").is_dir())
        self.assertEqual(os.readlink(path / "link"), "run")
        self.assertEqual((path / "run").stat().st_mode & 0o111, 0o111)
        for name in transfer.DEFAULT_EXCLUDES:
            self.assertFalse((path / name).exists())

    def test_copy_to_and_from_preserves_tree(self):
        self.populate()
        result = transfer.copy_to(self.sb, self.source, str(self.destination))
        self.assertGreater(result["entries"], 4)
        self.assert_tree(self.destination)
        returned = self.root / "returned"
        transfer.copy_from(self.sb, str(self.destination), returned)
        self.assert_tree(returned)
        self.assertEqual(len(self.sb.filesystem.removed), 2)
        self.assertTrue(all(not Path(p).exists() for p in self.sb.filesystem.removed))

    def test_empty_destination_is_allowed(self):
        self.destination.mkdir()
        (self.source / "hello").write_text("hello")
        transfer.copy_to(self.sb, self.source, str(self.destination))
        self.assertEqual((self.destination / "hello").read_text(), "hello")

    def test_existing_mount_root_stages_on_destination_filesystem(self):
        self.populate()
        self.destination.mkdir()
        archive = self.root / "archive.tar"
        transfer._pack(self.source, archive, transfer.DEFAULT_EXCLUDES)
        rename = os.rename
        replace = os.replace

        def same_filesystem(operation):
            def guarded(source, destination, *args, **kwargs):
                source_in_mount = Path(source).is_relative_to(self.destination)
                destination_in_mount = Path(destination).is_relative_to(self.destination)
                if source_in_mount != destination_in_mount:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return operation(source, destination, *args, **kwargs)

            return guarded

        with (
            patch("os.rename", side_effect=same_filesystem(rename)),
            patch("os.replace", side_effect=same_filesystem(replace)),
        ):
            transfer._extract(archive, self.destination, False)
        self.assert_tree(self.destination)
        self.assertEqual(list(self.destination.glob(".box-extract-*")), [])

    def test_validation_failure_leaves_existing_empty_mount_untouched(self):
        self.destination.mkdir()
        archive = self.root / "malicious.tar"
        with tarfile.open(archive, "w") as output:
            output.addfile(tarfile.TarInfo("valid"))
            output.addfile(tarfile.TarInfo("../escape"))
        with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
            transfer._extract(archive, self.destination, False)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_configured_symlink_volume_root_and_descendant(self):
        self.populate()
        backing = self.root / "volume-backing"
        backing.mkdir()
        mount = self.root / "volume-mount"
        mount.symlink_to(backing, target_is_directory=True)
        mounts = (str(mount),)
        result = transfer.copy_to(self.sb, self.source, str(mount), mounts=mounts)
        self.assertEqual(result["destination"], str(mount))
        self.assert_tree(backing)
        transfer.copy_to(self.sb, self.source, str(mount / "later"), mounts=mounts)
        self.assert_tree(backing / "later")
        pulled = self.root / "pulled"
        result = transfer.copy_from(self.sb, str(mount / "later"), pulled, mounts=mounts)
        self.assertEqual(result["source"], str(mount / "later"))
        self.assert_tree(pulled)

    def test_unconfigured_and_nested_symlink_paths_are_rejected(self):
        backing = self.root / "volume-backing"
        backing.mkdir()
        mount = self.root / "volume-mount"
        mount.symlink_to(backing, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "not a configured volume mount"):
            transfer.copy_to(self.sb, self.source, str(mount / "later"))
        (backing / "untrusted").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "not a configured volume mount"):
            transfer.copy_to(
                self.sb,
                self.source,
                str(mount / "untrusted" / "escaped"),
                mounts=(str(mount),),
            )
        self.assertFalse((self.root / "escaped").exists())

    def test_merge_into_nested_mount_copies_then_publishes_on_its_filesystem(self):
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "directory").mkdir()
        (nested / "directory" / "file").write_text("contents")
        (nested / "file").write_text("contents")
        (nested / "link").symlink_to("file")
        self.destination.mkdir()
        mount = self.destination / "nested"
        mount.mkdir()
        archive = self.root / "archive.tar"
        transfer._pack(self.source, archive, ())
        replace = os.replace

        def same_filesystem(source, destination, *args, **kwargs):
            if Path(source).is_relative_to(mount) != Path(destination).is_relative_to(mount):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return replace(source, destination, *args, **kwargs)

        with patch("os.replace", side_effect=same_filesystem):
            transfer._extract(archive, self.destination, True)
        self.assertEqual((mount / "directory" / "file").read_text(), "contents")
        self.assertEqual((mount / "file").read_text(), "contents")
        self.assertEqual(os.readlink(mount / "link"), "file")
        self.assertEqual(list(mount.glob(".box-publish-*")), [])

    def test_nonempty_destination_requires_overwrite(self):
        self.destination.mkdir()
        (self.destination / "keep").write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "--overwrite"):
            transfer.copy_to(self.sb, self.source, str(self.destination))
        self.assertEqual((self.destination / "keep").read_text(), "keep")

    def test_overwrite_merges_and_replaces_links_without_following(self):
        (self.source / "file").write_text("new")
        self.destination.mkdir()
        outside = self.root / "outside"
        outside.write_text("unchanged")
        (self.destination / "file").symlink_to(outside)
        (self.destination / "keep").write_text("keep")
        transfer.copy_to(self.sb, self.source, str(self.destination), overwrite=True)
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertEqual((self.destination / "file").read_text(), "new")
        self.assertFalse((self.destination / "file").is_symlink())
        self.assertEqual((self.destination / "keep").read_text(), "keep")

    def test_rejects_merge_through_directory_symlink(self):
        (self.source / "nested").mkdir()
        (self.source / "nested" / "new").write_text("new")
        self.destination.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.destination / "nested").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Cannot merge"):
            transfer.copy_to(self.sb, self.source, str(self.destination), overwrite=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_explicit_excludes(self):
        (self.source / "secret.txt").write_text("user excluded")
        (self.source / "keep").write_text("keep")
        transfer.copy_to(self.sb, self.source, str(self.destination), exclude=("*.txt",))
        self.assertFalse((self.destination / "secret.txt").exists())
        self.assertTrue((self.destination / "keep").exists())

    def test_external_and_absolute_source_links_are_rejected(self):
        for target in ("../outside", str(self.root / "outside")):
            with self.subTest(target=target):
                link = self.source / "link"
                link.symlink_to(target)
                with self.assertRaises(tarfile.FilterError):
                    transfer.copy_to(self.sb, self.source, str(self.destination))
                link.unlink()
                self.assertFalse(self.destination.exists())

    def test_hardlinked_source_files_copy_independently(self):
        (self.source / "a").write_text("contents")
        os.link(self.source / "a", self.source / "b")
        transfer.copy_to(self.sb, self.source, str(self.destination))
        self.assertEqual((self.destination / "a").read_text(), "contents")
        self.assertEqual((self.destination / "b").read_text(), "contents")

    def test_remote_path_validation(self):
        for path in ("relative", "/", "/tmp/../bad", "/tmp//bad", "/tmp/bad/", "/tmp/./bad"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transfer.copy_to(self.sb, self.source, path)

    def test_upload_and_download_failures_clean_owned_temp(self):
        self.sb.filesystem.fail_upload = True
        with self.assertRaisesRegex(OSError, "upload interrupted"):
            transfer.copy_to(self.sb, self.source, str(self.destination))
        self.sb.filesystem.fail_upload = False
        self.sb.filesystem.fail_download = True
        with self.assertRaisesRegex(OSError, "download interrupted"):
            transfer.copy_from(self.sb, str(self.source), self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.sb.filesystem.removed), 2)
        self.assertTrue(all(not Path(p).exists() for p in self.sb.filesystem.removed))

    def test_cleanup_error_preserves_primary_error(self):
        self.sb.filesystem.fail_upload = True
        self.sb.filesystem.fail_cleanup = True
        with self.assertRaisesRegex(OSError, "upload interrupted") as caught:
            transfer.copy_to(self.sb, self.source, str(self.destination))
        self.assertIn("cleanup interrupted", caught.exception.__notes__[0])

    def test_cleanup_failure_after_success_is_reported(self):
        self.sb.filesystem.fail_cleanup = True
        with self.assertRaisesRegex(RuntimeError, "could not remove owned"):
            transfer.copy_to(self.sb, self.source, str(self.destination))

    def test_malicious_archive_members_leave_destination_untouched(self):
        for name, kind, target in (
            ("../escape", tarfile.REGTYPE, ""),
            ("/absolute", tarfile.REGTYPE, ""),
            ("hard", tarfile.LNKTYPE, "../escape"),
            ("link", tarfile.SYMTYPE, "../escape"),
            ("device", tarfile.CHRTYPE, ""),
        ):
            with self.subTest(name=name):
                archive = self.root / "malicious.tar"
                with tarfile.open(archive, "w") as out:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = target
                    out.addfile(member)
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    transfer._extract(archive, self.destination, False)
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.root / "escape").exists())

    def test_archive_cannot_extract_through_even_internal_symlink(self):
        archive = self.root / "malicious.tar"
        with tarfile.open(archive, "w") as out:
            directory = tarfile.TarInfo("dir")
            directory.type = tarfile.DIRTYPE
            out.addfile(directory)
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "dir"
            out.addfile(link)
            out.addfile(tarfile.TarInfo("link/file"))
        with self.assertRaisesRegex(ValueError, "traverses a symlink"):
            transfer._extract(archive, self.destination, False)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
