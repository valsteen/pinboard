"""The Git adapter's target-content read compares a recorded diff without writing repository state."""

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError

COMMIT_DATE = "2030-01-02T03:04:05Z"


class TargetContentReadTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.project = Path(self._directory.name) / "project"
        self.project.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.project / "reviewed.txt").write_text("alpha\nbeta\n")
        self.git("add", "reviewed.txt")
        self.base = self.commit("base")
        (self.project / "reviewed.txt").write_text("alpha\ngamma\n")
        self.git("add", "reviewed.txt")
        self.changed = self.commit("change")
        self.diff = subprocess.run(
            ["git", "diff", "--binary", self.base, self.changed],
            cwd=self.project,
            check=True,
            capture_output=True,
        ).stdout

    def git(self, *arguments: str, environment: dict[str, str] | None = None) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                *arguments,
            ],
            cwd=self.project,
            env={**os.environ, **(environment or {})},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message: str) -> str:
        dated = {"GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE}
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "commit",
                "-q",
                "-m",
                message,
            ],
            cwd=self.project,
            env={**os.environ, **dated},
            check=True,
            capture_output=True,
        )
        return self.git("rev-parse", "HEAD")

    def test_present_and_not_present_targets_report_their_resolved_commits(self) -> None:
        present = root.observe_target_content(self.project, "main", self.diff)
        self.assertEqual(root.TargetContentPresent(self.changed), present)
        absent = root.observe_target_content(self.project, self.base, self.diff)
        self.assertEqual(root.TargetContentNotPresent(self.base), absent)

    def test_empty_recorded_diff_names_the_target_without_comparing(self) -> None:
        self.assertEqual(
            root.TargetContentUnchanged(self.changed), root.observe_target_content(self.project, "main", b"")
        )

    def test_unknown_target_is_observed_as_unresolved_not_an_error(self) -> None:
        self.assertEqual(
            root.TargetUnresolved("no-such-target"),
            root.observe_target_content(self.project, "no-such-target", self.diff),
        )

    def test_non_git_directory_is_a_root_error(self) -> None:
        outside = Path(self._directory.name) / "outside"
        outside.mkdir()
        with self.assertRaises(RootError):
            root.observe_target_content(outside, "main", self.diff)

    def test_whitespace_configuration_does_not_change_the_verdict(self) -> None:
        self.git("config", "apply.whitespace", "error")
        self.git("config", "core.splitIndex", "true")
        self.assertEqual(
            root.TargetContentPresent(self.changed), root.observe_target_content(self.project, "main", self.diff)
        )

    def test_trailing_whitespace_with_error_configuration_still_reports_present(self) -> None:
        self.git("config", "apply.whitespace", "error")
        (self.project / "reviewed.txt").write_text("alpha\ngamma \n")
        self.git("add", "reviewed.txt")
        spaced = self.commit("Add trailing whitespace")
        diff = subprocess.run(
            ["git", "diff", "--binary", self.base, spaced], cwd=self.project, check=True, capture_output=True
        ).stdout
        self.assertIn(b"+gamma \n", diff)
        self.assertEqual(root.TargetContentPresent(spaced), root.observe_target_content(self.project, "main", diff))

    def test_read_only_git_directory_is_left_unchanged_and_leaves_no_temporary_index(self) -> None:
        def snapshot() -> str:
            digest = hashlib.sha256()
            for path in sorted(self.project.joinpath(".git").rglob("*")):
                if path.is_file():
                    digest.update(str(path.relative_to(self.project)).encode())
                    digest.update(path.read_bytes())
            return digest.hexdigest()

        before = snapshot()
        temporary = Path(tempfile.gettempdir())
        leftovers_before = {entry.name for entry in temporary.glob("pinboard-integration-*")}
        git_directory = self.project / ".git"
        modes = {path: path.stat().st_mode for path in git_directory.rglob("*") if path.is_dir()}
        for path in git_directory.rglob("*"):
            if path.is_dir():
                path.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: [path.chmod(mode) for path, mode in modes.items()])
        try:
            observed = root.observe_target_content(self.project, "main", self.diff)
        finally:
            for path, mode in modes.items():
                path.chmod(mode)
        self.assertEqual(root.TargetContentPresent(self.changed), observed)
        self.assertEqual(before, snapshot())
        leftovers_after = {entry.name for entry in temporary.glob("pinboard-integration-*")}
        self.assertLessEqual(leftovers_after, leftovers_before)


if __name__ == "__main__":
    unittest.main()
