"""The Git adapter's read-only reverse-apply check of reviewed diff bytes against a named target."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError

FIXED_DATE = "2030-01-01T00:00:00+00:00"


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Pinboard Tests", "-c", "user.email=pinboard@example.invalid", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _depth(path: Path) -> int:
    return len(path.parts)


class GitIntegrationReadTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        dates = patch.dict(os.environ, {"GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE})
        dates.start()
        self.addCleanup(dates.stop)
        self.temporary_root = Path(tempfile.mkdtemp()).resolve()
        isolated = patch.object(tempfile, "tempdir", str(self.temporary_root))
        self.project = Path(tempfile.mkdtemp()).resolve()
        git(self.project, "init", "-q", "-b", "main")
        (self.project / "notes.txt").write_text("".join(f"line {number}\n" for number in range(1, 21)))
        (self.project / "original-name.txt").write_text("renamed content\n" * 8)
        self.base = self.commit("base")
        git(self.project, "mv", "original-name.txt", "renamed.txt")
        lines = (self.project / "notes.txt").read_text().splitlines(keepends=True)
        lines[1] = "reviewed line two  \n"
        (self.project / "notes.txt").write_text("".join(lines))
        (self.project / "image.bin").write_bytes(bytes(range(256)))
        git(self.project, "add", "--all")
        self.diff = subprocess.run(
            ["git", "diff", "--binary", "--cached", "HEAD"], cwd=self.project, check=True, capture_output=True
        ).stdout
        self.candidate = self.commit("candidate")
        isolated.start()
        self.addCleanup(isolated.stop)

    def commit(self, message: str) -> str:
        git(self.project, "add", "--all")
        git(self.project, "commit", "-q", "-m", message)
        return git(self.project, "rev-parse", "HEAD")

    def git_state(self) -> dict[str, bytes]:
        metadata = self.project / ".git"
        return {
            str(path.relative_to(metadata)): path.read_bytes() for path in sorted(metadata.rglob("*")) if path.is_file()
        } | {
            "worktree": subprocess.run(
                ["git", "status", "--porcelain", "--ignored"], cwd=self.project, check=True, capture_output=True
            ).stdout
        }

    def test_present_after_squash_and_non_overlapping_edit_and_absent_at_base_or_overlap(self) -> None:
        git(self.project, "checkout", "-q", "-b", "target", self.base)
        self.assertEqual(
            root.TargetLacksDiff(self.base), root.observe_target_content(self.project, "target", self.diff)
        )
        git(self.project, "merge", "-q", "--squash", self.candidate)
        squashed = self.commit("squash")
        self.assertEqual(
            root.TargetContainsDiff(squashed), root.observe_target_content(self.project, "target", self.diff)
        )
        lines = (self.project / "notes.txt").read_text().splitlines(keepends=True)
        lines[17] = "later edit far from the reviewed line\n"
        (self.project / "notes.txt").write_text("".join(lines))
        later = self.commit("later")
        self.assertEqual(root.TargetContainsDiff(later), root.observe_target_content(self.project, later, self.diff))
        lines[1] = "overlapping edit\n"
        (self.project / "notes.txt").write_text("".join(lines))
        overlapping = self.commit("overlap")
        self.assertEqual(
            root.TargetLacksDiff(overlapping), root.observe_target_content(self.project, "target", self.diff)
        )

    def test_unresolved_target_and_non_git_directory(self) -> None:
        unresolved = root.observe_target_content(self.project, "missing-branch", self.diff)
        self.assertIsInstance(unresolved, root.UnresolvedTarget)
        assert isinstance(unresolved, root.UnresolvedTarget)
        self.assertEqual("missing-branch", unresolved.target)
        self.assertIsInstance(root.resolve_target_revision(self.project, "missing-branch"), root.UnresolvedTarget)
        with self.assertRaises(RootError):
            root.observe_target_content(Path(tempfile.mkdtemp()).resolve(), "main", self.diff)

    def test_whitespace_configuration_does_not_change_the_verdict(self) -> None:
        git(self.project, "config", "apply.whitespace", "error")
        git(self.project, "config", "apply.ignoreWhitespace", "change")
        git(self.project, "config", "core.splitIndex", "true")
        self.assertIn(b"two  \n", self.diff)
        self.assertEqual(
            root.TargetContainsDiff(self.candidate), root.observe_target_content(self.project, "main", self.diff)
        )
        self.assertEqual(
            root.TargetLacksDiff(self.base), root.observe_target_content(self.project, self.base, self.diff)
        )

    def test_read_only_git_metadata_stays_unchanged_and_no_temporary_directory_remains(self) -> None:
        metadata = self.project / ".git"
        before = self.git_state()
        objects = git(self.project, "count-objects", "-v")
        modes = {path: path.stat().st_mode for path in (metadata, *metadata.rglob("*"))}
        for path in sorted(modes, key=_depth, reverse=True):
            path.chmod(stat.S_IMODE(modes[path]) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        try:
            self.assertEqual(
                root.TargetContainsDiff(self.candidate), root.observe_target_content(self.project, "main", self.diff)
            )
            self.assertEqual(
                root.TargetLacksDiff(self.base), root.observe_target_content(self.project, self.base, self.diff)
            )
        finally:
            for path, mode in modes.items():
                path.chmod(stat.S_IMODE(mode))
        self.assertEqual(before, self.git_state())
        self.assertEqual(objects, git(self.project, "count-objects", "-v"))
        self.assertEqual([], list(self.temporary_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
