"""The temporary-index content observation preserves the entire checkout."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError


class IntegrationContentTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "repository"
        self.project.mkdir()
        dates = patch.dict(
            os.environ,
            {"GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00"},
        )
        dates.start()
        self.addCleanup(dates.stop)
        self.git("init", "-b", "main")
        (self.project / "text.txt").write_text("old\n", encoding="utf-8")
        (self.project / "binary.dat").write_bytes(b"\x00old\xff")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.project / "text.txt").rename(self.project / "renamed.txt")
        (self.project / "renamed.txt").write_text("new trailing whitespace  \n", encoding="utf-8")
        (self.project / "binary.dat").write_bytes(b"\x00new\xff")
        self.git("add", ".")
        self.diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=self.project, check=True, capture_output=True
        ).stdout
        self.git("commit", "-m", "reviewed content")
        self.tip = self.git("rev-parse", "HEAD")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", *arguments],
            cwd=self.project,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def inventory(self) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(self.project)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.project.rglob("*")
            if path.is_file()
        }

    def test_content_presence_absence_and_unresolved_target(self) -> None:
        self.assertEqual(
            root.ContentObservation(self.tip, True), root.read_integration_content(self.project, "main", self.diff)
        )
        self.assertEqual(
            root.ContentObservation(self.base, False), root.read_integration_content(self.project, self.base, self.diff)
        )
        self.assertEqual(
            root.UnresolvedTarget("unknown"), root.read_integration_content(self.project, "unknown", self.diff)
        )
        with self.assertRaises(RootError):
            root.read_integration_content(Path(self.temporary.name), "main", self.diff)

    def test_read_only_git_and_whitespace_split_index_configuration_leave_every_file_unchanged(self) -> None:
        self.git("config", "apply.whitespace", "error")
        self.git("config", "core.splitIndex", "true")
        self.git("update-index", "--split-index")
        before = self.inventory()
        metadata = self.project / ".git"
        modes = {path: path.stat().st_mode for path in metadata.rglob("*")}
        for path in modes:
            path.chmod(0o500 if path.is_dir() else 0o400)
        metadata.chmod(0o500)
        temporary_parent = Path(self.temporary.name) / "indexes"
        temporary_parent.mkdir()
        actual_temporary = root.TemporaryDirectory

        def private_index(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
            return actual_temporary(dir=temporary_parent, prefix=prefix)

        try:
            with patch(
                "pinboard.adapters.files.root.TemporaryDirectory",
                side_effect=private_index,
            ):
                observed = root.read_integration_content(self.project, "main", self.diff)
            self.assertEqual(root.ContentObservation(self.tip, True), observed)
            self.assertEqual(before, self.inventory())
            self.assertEqual([], list(temporary_parent.iterdir()))
        finally:
            metadata.chmod(0o700)
            for path, mode in modes.items():
                path.chmod(mode)

    def test_empty_diff_does_not_create_a_temporary_index(self) -> None:
        with patch(
            "pinboard.adapters.files.root.TemporaryDirectory", side_effect=AssertionError("no index for empty diff")
        ):
            self.assertEqual(
                root.ContentObservation(self.tip, True), root.read_integration_content(self.project, "main", b"")
            )

    def test_context_spacing_changes_remain_absent_under_user_ignore_whitespace_setting(self) -> None:
        (self.project / "context.txt").write_text("context line\nold\ncontext line\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "context base")
        base = self.git("rev-parse", "HEAD")
        (self.project / "context.txt").write_text("context line\nnew\ncontext line\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed context change")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, "HEAD"], cwd=self.project, capture_output=True, check=True
        ).stdout
        (self.project / "context.txt").write_text("  context line\nnew\ncontext line\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "later context spacing")
        tip = self.git("rev-parse", "HEAD")
        for setting in ("no", "change"):
            with self.subTest(setting=setting):
                self.git("config", "apply.ignoreWhitespace", setting)
                self.assertEqual(
                    root.ContentObservation(tip, False), root.read_integration_content(self.project, "main", diff)
                )
