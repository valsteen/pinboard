"""Content observations over real Git trees, without changing repository state."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from pinboard.adapters.files import root
from pinboard.adapters.files.errors import RootError
from pinboard.application.integration import ContentPresence


class IntegrationContentTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.environment = {
            **os.environ,
            "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00",
        }
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Integration tests")
        self.git("config", "user.email", "integration@example.invalid")
        self.file = self.project / "text.txt"
        self.file.write_text("".join(f"line {i}\n" for i in range(30)))
        (self.project / "binary").write_bytes(b"\x00old\xff")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.file.write_text(self.file.read_text().replace("line 2\n", "reviewed 2  \n"))
        self.git("mv", "binary", "renamed")
        (self.project / "renamed").write_bytes(b"\x00new\xff")
        self.diff = self.git("diff", "--binary", "HEAD")
        self.git("add", ".")
        self.git("commit", "-m", "candidate")

    def git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", *args], cwd=self.project, env=self.environment, check=True, capture_output=True
        ).stdout

    def assert_presence(self, target: str, presence: ContentPresence, diff: bytes) -> None:
        actual = root.read_integration_content(self.project, target, diff)
        self.assertIsInstance(actual, root.IntegrationTarget)
        assert isinstance(actual, root.IntegrationTarget)
        self.assertEqual(presence, actual.presence)
        self.assertEqual(self.git("rev-parse", f"{target}^{{commit}}").decode().strip(), actual.revision)

    def test_target_content_and_later_edits(self) -> None:
        self.assert_presence("main", ContentPresence.PRESENT, self.diff)
        self.assert_presence(self.base, ContentPresence.NOT_PRESENT, self.diff)
        self.file.write_text(self.file.read_text().replace("line 25\n", "later 25\n"))
        self.git("commit", "-am", "non-overlap")
        self.assert_presence("main", ContentPresence.PRESENT, self.diff)
        self.file.write_text(self.file.read_text().replace("reviewed 2  \n", "overlapping 2\n"))
        self.git("commit", "-am", "overlap")
        self.assert_presence("main", ContentPresence.NOT_PRESENT, self.diff)

    def test_internal_context_spacing_is_not_ignored_by_user_configuration(self) -> None:
        self.file.write_text(self.file.read_text().replace("line 1\n", "line    1\n"))
        self.git("commit", "-am", "change context spacing")
        target = self.git("rev-parse", "HEAD").decode().strip()
        for setting in ("no", "change"):
            with self.subTest(setting=setting):
                self.git("config", "apply.ignoreWhitespace", setting)
                self.assert_presence(target, ContentPresence.NOT_PRESENT, self.diff)

    def test_empty_diff_resolves_target_without_comparison_or_temporary_index(self) -> None:
        with patch.object(root.tempfile, "TemporaryDirectory", side_effect=AssertionError("empty diff needs no index")):
            self.assert_presence("main", ContentPresence.NO_CHANGE, b"")
            self.assertEqual(
                root.UnresolvedIntegrationTarget("absent"), root.read_integration_content(self.project, "absent", b"")
            )

    def test_unresolved_and_non_git_targets(self) -> None:
        self.assertEqual(
            root.UnresolvedIntegrationTarget("absent"), root.read_integration_content(self.project, "absent", self.diff)
        )
        with self.assertRaises(RootError):
            root.read_integration_content(Path(self.temporary.name), "main", self.diff)

    def test_read_only_metadata_and_hostile_whitespace_and_split_index_settings(self) -> None:
        self.git("config", "apply.whitespace", "error")
        self.git("config", "core.splitIndex", "true")
        self.git("update-index", "--split-index")
        scratch = Path(self.temporary.name) / "scratch"
        scratch.mkdir()
        before = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        metadata = [*(self.project / ".git").rglob("*"), self.project / ".git"]
        modes = {p: p.stat().st_mode for p in metadata}
        for p in metadata:
            p.chmod(p.stat().st_mode & ~0o222)
        try:
            with patch.object(root.tempfile, "tempdir", str(scratch)):
                self.assert_presence("main", ContentPresence.PRESENT, self.diff)
            after = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual([], list(scratch.iterdir()))
        finally:
            for p, mode in modes.items():
                p.chmod(mode)

    def test_temporary_index_failure_and_invalid_patch_preserve_root_error(self) -> None:
        with (
            patch.object(
                root.tempfile, "TemporaryDirectory", side_effect=PermissionError("temporary directory denied")
            ),
            self.assertRaisesRegex(RootError, "temporary directory denied"),
        ):
            root.read_integration_content(self.project, "main", self.diff)
        with self.assertRaises(RootError):
            root.read_integration_content(self.project, "main", b"not a patch\n")
