"""The Git adapter's target-content read observes reviewed changes without changing the repository."""

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.root import TargetContent, UnresolvedTarget, observe_target_content

FIXED_DATE = "2030-01-02T03:04:05+00:00"
GIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Pinboard Tests",
    "GIT_AUTHOR_EMAIL": "pinboard@example.invalid",
    "GIT_COMMITTER_NAME": "Pinboard Tests",
    "GIT_COMMITTER_EMAIL": "pinboard@example.invalid",
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
}


def run_git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env={**os.environ, **GIT_ENVIRONMENT},
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    """Record every file's mode and content digest under one directory."""

    return {
        str(path.relative_to(root)): (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TargetContentTest(unittest.TestCase):
    def repository(self) -> tuple[Path, str, bytes]:
        """Create a base commit and a reviewed change whose diff, reversed, restores trailing whitespace."""

        repository = Path(tempfile.mkdtemp()).resolve()
        run_git(repository, "init", "-b", "main")
        (repository / "reviewed.txt").write_text("one\ntwo\nthree  \nfour\nfive\nsix\nseven\n", encoding="utf-8")
        (repository / "other.txt").write_text("unrelated\n", encoding="utf-8")
        (repository / "renamed.txt").write_text("".join(f"kept line {n}\n" for n in range(20)), encoding="utf-8")
        run_git(repository, "add", "--all")
        run_git(repository, "commit", "-m", "base")
        base = run_git(repository, "rev-parse", "HEAD")
        run_git(repository, "switch", "-c", "feature")
        (repository / "reviewed.txt").write_text("one\ntwo\nTHREE\nfour\nfive\nsix\nseven\n", encoding="utf-8")
        (repository / "image.bin").write_bytes(bytes(range(256)))
        run_git(repository, "mv", "renamed.txt", "moved.txt")
        run_git(repository, "add", "--all")
        run_git(repository, "commit", "-m", "candidate")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, "HEAD", "--"], cwd=repository, check=True, capture_output=True
        ).stdout
        run_git(repository, "switch", "main")
        return repository, base, diff

    def squash(self, repository: Path) -> str:
        run_git(repository, "merge", "--squash", "feature")
        run_git(repository, "commit", "-m", "squash")
        return run_git(repository, "rev-parse", "HEAD")

    def test_squash_merged_change_is_present_and_the_base_is_not(self) -> None:
        repository, base, diff = self.repository()
        self.assertIn(b"rename from renamed.txt\nrename to moved.txt\n", diff)
        self.assertIn(b"GIT binary patch", diff)
        self.assertEqual(TargetContent(base, False), observe_target_content(repository, "main", diff))
        squashed = self.squash(repository)
        self.assertEqual(TargetContent(squashed, True), observe_target_content(repository, "main", diff))
        self.assertEqual(TargetContent(base, False), observe_target_content(repository, base, diff))

    def test_later_overlapping_edit_hides_the_change_but_a_non_overlapping_edit_does_not(self) -> None:
        repository, _base, diff = self.repository()
        self.squash(repository)
        (repository / "reviewed.txt").write_text("one\ntwo\nTHREE\nfour\nfive\nsix\nSEVEN\n", encoding="utf-8")
        run_git(repository, "commit", "-am", "later non-overlapping edit")
        later = run_git(repository, "rev-parse", "HEAD")
        self.assertEqual(TargetContent(later, True), observe_target_content(repository, "main", diff))
        (repository / "reviewed.txt").write_text("one\nTWO\nTHREE\nfour\nfive\nsix\nSEVEN\n", encoding="utf-8")
        run_git(repository, "commit", "-am", "later overlapping edit")
        overlapping = run_git(repository, "rev-parse", "HEAD")
        self.assertEqual(TargetContent(overlapping, False), observe_target_content(repository, "main", diff))

    def test_unknown_revision_is_unresolved_and_a_non_git_directory_raises(self) -> None:
        repository, _base, diff = self.repository()
        self.assertEqual(UnresolvedTarget("missing"), observe_target_content(repository, "missing", diff))
        blob = run_git(repository, "rev-parse", "main:other.txt")
        self.assertEqual(UnresolvedTarget(blob), observe_target_content(repository, blob, diff))
        with self.assertRaisesRegex(RootError, "PROJECT_GIT_CHECKOUT_UNAVAILABLE"):
            observe_target_content(Path(tempfile.mkdtemp()), "main", diff)
        with self.assertRaisesRegex(RootError, "PROJECT_GIT_CHECKOUT_UNAVAILABLE"):
            observe_target_content(repository, "main", b"not a patch\n")

    def test_whitespace_and_split_index_configuration_do_not_change_the_verdict(self) -> None:
        repository, _base, diff = self.repository()
        squashed = self.squash(repository)
        for key, value in (
            ("apply.whitespace", "error"),
            ("apply.ignoreWhitespace", "change"),
            ("core.splitIndex", "true"),
        ):
            run_git(repository, "config", key, value)
        self.assertEqual(TargetContent(squashed, True), observe_target_content(repository, "main", diff))

    def test_read_only_git_directory_stays_unchanged_and_no_temporary_directory_remains(self) -> None:
        repository, _base, diff = self.repository()
        squashed = self.squash(repository)
        git_directory = repository / ".git"
        before_git = snapshot_tree(git_directory)
        before_worktree = {key: value for key, value in snapshot_tree(repository).items() if not key.startswith(".git")}
        before_objects = run_git(repository, "count-objects", "-v")
        before_head = run_git(repository, "rev-parse", "HEAD")
        writable = [path for path in (git_directory, *git_directory.rglob("*")) if not path.is_symlink()]
        modes = {path: path.stat().st_mode for path in writable}
        scratch = Path(tempfile.mkdtemp())
        try:
            for path in writable:
                path.chmod(modes[path] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            with patch.object(tempfile, "tempdir", str(scratch)):
                present = observe_target_content(repository, "main", diff)
                absent = observe_target_content(repository, "HEAD~1", diff)
        finally:
            for path in reversed(writable):
                path.chmod(modes[path])
        self.assertEqual(TargetContent(squashed, True), present)
        self.assertIsInstance(absent, TargetContent)
        assert isinstance(absent, TargetContent)
        self.assertFalse(absent.present)
        self.assertEqual([], list(scratch.iterdir()))
        self.assertEqual(before_git, snapshot_tree(git_directory))
        self.assertEqual(
            before_worktree,
            {key: value for key, value in snapshot_tree(repository).items() if not key.startswith(".git")},
        )
        self.assertEqual(before_objects, run_git(repository, "count-objects", "-v"))
        self.assertEqual(before_head, run_git(repository, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
