"""The Git adapter's target-content read leaves the repository unchanged and reports reviewed content as Git applies it."""

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files.errors import RootError, RootErrorCode
from pinboard.adapters.files.root import (
    TargetContentNotPresent,
    TargetContentPresent,
    TargetUnresolved,
    observe_target_content,
)

FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+0000",
}
BASE_TEXT = "alpha\nbeta\ngamma\n"
REVIEWED_TEXT = "alpha\nBETA\ngamma\n"


def tree_state(root: Path, *, include_git: bool) -> dict[str, tuple[int, int, str]]:
    """Capture every file's size, modification time, and digest, so any write shows."""

    state: dict[str, tuple[int, int, str]] = {}
    for directory, directories, files in os.walk(root):
        if not include_git and ".git" in directories:
            directories.remove(".git")
        for name in files:
            path = Path(directory) / name
            observed = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(observed.st_mode) else ""
            state[str(path.relative_to(root))] = (observed.st_size, observed.st_mtime_ns, digest)
    return state


class TargetContentReadTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        environment = patch.dict(os.environ, FIXED_GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def repository(self, *config: tuple[str, str]) -> Path:
        project = Path(tempfile.mkdtemp()).resolve()
        self.git(project, "init", "-b", "main")
        for key, value in config:
            self.git(project, "config", key, value)
        (project / "tracked.txt").write_text(BASE_TEXT, encoding="utf-8")
        self.git(project, "add", "--all")
        self.git(project, "commit", "-m", "base")
        return project

    def reviewed_diff(self, project: Path) -> bytes:
        (project / "tracked.txt").write_text(REVIEWED_TEXT, encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        self.git(project, "checkout", "--", "tracked.txt")
        return diff

    def integrate(self, project: Path, branch: str) -> str:
        self.git(project, "switch", "-c", branch)
        (project / "tracked.txt").write_text(REVIEWED_TEXT, encoding="utf-8")
        self.git(project, "commit", "--all", "-m", "integrate")
        revision = self.git(project, "rev-parse", "HEAD")
        self.git(project, "switch", "main")
        return revision

    def test_reports_present_not_present_and_unresolved_for_each_kind_of_revision_name(self) -> None:
        project = self.repository()
        base = self.git(project, "rev-parse", "HEAD")
        diff = self.reviewed_diff(project)
        integrated = self.integrate(project, "integrated")
        self.git(project, "tag", "released", integrated)
        self.assertEqual(TargetContentNotPresent(base), observe_target_content(project, "main", diff))
        for name in ("integrated", "released", integrated, "integrated~0", f"{integrated[:12]}"):
            with self.subTest(target=name):
                self.assertEqual(TargetContentPresent(integrated), observe_target_content(project, name, diff))
        for name in ("no-such-ref", "main:tracked.txt", "main..integrated", "integrated^{tree}"):
            with self.subTest(unresolved=name):
                self.assertEqual(TargetUnresolved(name), observe_target_content(project, name, diff))
        self.assertEqual(TargetContentPresent(base), observe_target_content(project, "main", b""))
        self.assertEqual(TargetUnresolved("no-such-ref"), observe_target_content(project, "no-such-ref", b""))

    def test_renames_and_binary_files_are_recognized_after_a_squash_merge(self) -> None:
        project = self.repository()
        (project / "blob.bin").write_bytes(bytes(range(256)))
        self.git(project, "add", "blob.bin")
        self.git(project, "commit", "-m", "binary context")
        self.git(project, "switch", "-c", "candidate")
        (project / "blob.bin").write_bytes(bytes(reversed(range(256))))
        self.git(project, "mv", "tracked.txt", "renamed.txt")
        (project / "renamed.txt").write_text(REVIEWED_TEXT, encoding="utf-8")
        self.git(project, "add", "--all")
        diff = subprocess.run(
            ["git", "diff", "--binary", "--cached", "main", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        self.assertIn(b"GIT binary patch", diff)
        self.assertIn(b"rename from tracked.txt", diff)
        self.git(project, "commit", "-m", "candidate")
        self.git(project, "switch", "main")
        self.assertIsInstance(observe_target_content(project, "main", diff), TargetContentNotPresent)
        self.git(project, "merge", "--squash", "candidate")
        self.git(project, "commit", "-m", "squash")
        self.assertIsInstance(observe_target_content(project, "main", diff), TargetContentPresent)

    def test_a_directory_outside_any_git_checkout_is_a_root_error(self) -> None:
        outside = Path(tempfile.mkdtemp()).resolve()
        with self.assertRaises(RootError) as rejected:
            observe_target_content(outside, "main", b"diff")
        self.assertEqual(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, rejected.exception.code)
        self.assertIn("not a git repository", rejected.exception.detail)

    def test_the_verdict_ignores_the_users_whitespace_policy(self) -> None:
        project = self.repository(("apply.whitespace", "error"), ("core.whitespace", "trailing-space"))
        (project / "tracked.txt").write_text("padded \n", encoding="utf-8")
        self.git(project, "commit", "--all", "-m", "padded base")
        (project / "tracked.txt").write_text("also padded \n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=project, check=True, capture_output=True
        ).stdout
        self.git(project, "commit", "--all", "-m", "integrate")
        self.assertIn(b" \n", diff)
        control = subprocess.run(
            ["git", "apply", "--check", "--reverse", "-"], cwd=project, input=diff, capture_output=True, check=False
        )
        self.assertNotEqual(0, control.returncode)
        self.assertIsInstance(observe_target_content(project, "main", diff), TargetContentPresent)

    def test_read_only_git_metadata_stays_unchanged_and_no_temporary_directory_remains(self) -> None:
        project = self.repository(("core.splitIndex", "true"))
        diff = self.reviewed_diff(project)
        integrated = self.integrate(project, "integrated")
        self.git(project, "update-ref", "refs/remotes/origin/main", integrated)
        (project / "untracked.txt").write_text("left alone\n", encoding="utf-8")
        before_head = self.git(project, "rev-parse", "HEAD")
        before_git = tree_state(project / ".git", include_git=True)
        before_tree = tree_state(project, include_git=False)
        before_objects = sorted(path.name for path in (project / ".git" / "objects").rglob("*") if path.is_file())
        scratch = Path(tempfile.mkdtemp()).resolve()
        self.make_read_only(project / ".git")
        with patch.object(tempfile, "tempdir", str(scratch)):
            present = observe_target_content(project, "origin/main", diff)
            absent = observe_target_content(project, "main", diff)
        self.assertEqual(TargetContentPresent(integrated), present)
        self.assertIsInstance(absent, TargetContentNotPresent)
        self.assertEqual([], list(scratch.iterdir()))
        self.assertEqual(before_git, tree_state(project / ".git", include_git=True))
        self.assertEqual(before_tree, tree_state(project, include_git=False))
        self.assertEqual(
            before_objects, sorted(path.name for path in (project / ".git" / "objects").rglob("*") if path.is_file())
        )
        self.assertEqual(before_head, self.git(project, "rev-parse", "HEAD"))

    def make_read_only(self, git_directory: Path) -> None:
        def restore() -> None:
            for directory, directories, files in os.walk(git_directory):
                for name in (*directories, *files):
                    (Path(directory) / name).chmod(0o700)
            git_directory.chmod(0o700)

        self.addCleanup(restore)
        for directory, directories, files in os.walk(git_directory, topdown=False):
            for name in files:
                (Path(directory) / name).chmod(0o444)
            for name in directories:
                (Path(directory) / name).chmod(0o555)
        git_directory.chmod(0o555)


if __name__ == "__main__":
    unittest.main()
