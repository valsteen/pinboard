import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.legacy_storage import StorageLocation, observe_storage_location
from pinboard.adapters.files.root import ensure_git_exclude
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import NewArtifact
from pinboard.domain import work_models
from pinboard.interfaces.cli import main
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, JsonObject


class StorageMigrationTests(unittest.TestCase):
    def run_cli(self, project: Path, *arguments: str) -> tuple[int, JsonObject]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(("--project-root", str(project), *arguments, "--json"))
        return code, json.loads(output.getvalue())

    def make_legacy(self, project: Path) -> Path:
        self.assertEqual(0, self.run_cli(project, "init")[0])
        (project / ".codex").mkdir()
        legacy = project / ".codex" / "pinboard"
        (project / ".pinboard").rename(legacy)
        return legacy

    def test_migration_preserves_accepted_artifact_and_reloaded_nonempty_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.make_legacy(project)
            (legacy.parent / "config.toml").write_text("unrelated")
            roots = resolve_durable_roots(project, legacy)
            published = ArtifactRepository(roots).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "kept", 1, ".txt", b"immutable\n")
            )
            store = SQLiteWorkStore(roots.database_path)
            accepted = expect_success(store.accept_artifact_reference(legacy, published.reference, SQLITE_NOW))
            before = store.validated_snapshot()
            database_bytes = roots.database_path.read_bytes()
            code, receipt = self.run_cli(project, "migrate-storage")
            self.assertEqual(0, code, receipt)
            self.assertEqual(Path("../.pinboard"), legacy.readlink())
            self.assertEqual(database_bytes, (legacy / "state.sqlite3").read_bytes())
            self.assertEqual(before, SQLiteWorkStore(project / ".pinboard" / "state.sqlite3").validated_snapshot())
            self.assertEqual(b"immutable\n", (legacy / published.reference.selector).read_bytes())
            verified, result = self.run_cli(
                project,
                "artifact",
                "verify",
                "--artifact-ref-id",
                str(accepted.reference.artifact_ref_id),
                "--selector",
                published.reference.selector,
                "--sha256",
                published.reference.content_sha256,
                "--size-bytes",
                str(published.reference.size_bytes),
            )
            self.assertEqual(0, verified, result)
            self.assertTrue(result["verified"])
            self.assertEqual(0, self.run_cli(project, "--work-root", str(legacy), "init")[0])
            code, repeated = self.run_cli(project, "migrate-storage")
            self.assertEqual((0, [], "unchanged"), (code, repeated["changed_paths"], repeated["effect_disposition"]))
            self.assertEqual("unrelated", (legacy.parent / "config.toml").read_text())
            for path in (".pinboard/state.sqlite3", ".codex/pinboard"):
                self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", path], cwd=project).returncode)
            self.assertNotEqual(
                0, subprocess.run(["git", "check-ignore", "-q", ".codex/config.toml"], cwd=project).returncode
            )

    def test_partial_move_and_alias_failure_can_be_retried_without_replacing_data(self) -> None:
        for operation, expected_changed in (("rename", False), ("symlink_to", True)):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                legacy = self.make_legacy(project)
                before = (legacy / "state.sqlite3").read_bytes()
                with patch.object(Path, operation, side_effect=PermissionError("injected failure")):
                    code, failure = self.run_cli(project, "migrate-storage")
                self.assertEqual(12, code)
                self.assertEqual(expected_changed, bool(failure["changed_paths"]))
                self.assertEqual("run-migration", failure["retry_disposition"])
                self.assertEqual(0, self.run_cli(project, "migrate-storage")[0])
                self.assertEqual(before, (legacy / "state.sqlite3").read_bytes())

    def test_conflicts_never_initialize_or_overwrite_and_explicit_roots_remain_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            legacy = self.make_legacy(project)
            neutral = project / ".pinboard"
            neutral.mkdir()
            for command in ("init", "status", "migrate-storage"):
                code, receipt = self.run_cli(project, command)
                self.assertNotEqual(0, code)
                self.assertEqual("unchanged", receipt["effect_disposition"])
            self.assertTrue((legacy / "state.sqlite3").is_file())
            self.assertEqual(0, self.run_cli(project, "--work-root", str(project / ".codex" / "custom"), "init")[0])
            self.assertEqual(0, self.run_cli(project, "tool-contract")[0])

    def test_alias_restoration_rejects_symlinked_parent_without_affecting_neutral_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, other = root / "project", root / "other"
            project.mkdir()
            other.mkdir()
            self.assertEqual(0, self.run_cli(project, "init")[0])
            (project / ".codex").symlink_to(other, target_is_directory=True)
            before = (project / ".pinboard" / "state.sqlite3").read_bytes()
            self.assertEqual(0, self.run_cli(project, "status")[0])
            code, receipt = self.run_cli(project, "migrate-storage")
            self.assertEqual(2, code, receipt)
            self.assertEqual("unchanged", receipt["effect_disposition"])
            self.assertEqual([], receipt["changed_paths"])
            self.assertEqual("correct-roots", receipt["retry_disposition"])
            self.assertEqual([], list(other.iterdir()))
            self.assertEqual(before, (project / ".pinboard" / "state.sqlite3").read_bytes())
            self.assertEqual(0, self.run_cli(project, "status")[0])

    def test_alias_in_symlinked_parent_is_not_the_project_compatibility_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, other = root / "project", root / "other"
            project.mkdir()
            other.mkdir()
            (project / ".pinboard").mkdir()
            (other / "pinboard").symlink_to("../.pinboard")
            (project / ".codex").symlink_to(other, target_is_directory=True)
            self.assertEqual(StorageLocation.CONFLICT, observe_storage_location(project))

    def test_unexpected_legacy_entries_and_wrong_aliases_never_create_a_board(self) -> None:
        for entry in ("file", "wrong-alias"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                (project / ".codex").mkdir()
                legacy = project / ".codex" / "pinboard"
                if entry == "file":
                    legacy.write_bytes(b"retain me")
                else:
                    legacy.symlink_to("../elsewhere")
                for command in ("init", "migrate-storage"):
                    code, receipt = self.run_cli(project, command)
                    self.assertNotEqual(0, code)
                    self.assertEqual("unchanged", receipt["effect_disposition"])
                self.assertFalse((project / ".pinboard").exists())
                self.assertTrue(legacy.exists(follow_symlinks=False))

    def test_failure_after_exclusion_publication_reports_that_effect_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.make_legacy(project)
            exclude = project / ".git" / "info" / "exclude"
            exclude.write_bytes(b"/.codex/pinboard/\n")

            def fail_after_exclusion(repository: Path, entry: bytes) -> Path | None:
                ensure_git_exclude(repository, entry)
                raise OSError("injected after exclusion publication")

            with patch("pinboard.adapters.files.legacy_storage.ensure_git_exclude", side_effect=fail_after_exclusion):
                code, failure = self.run_cli(project, "migrate-storage")
            self.assertEqual(12, code)
            changed_paths = failure["changed_paths"]
            assert isinstance(changed_paths, list)
            self.assertIn(str(exclude), changed_paths)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(0, self.run_cli(project, "migrate-storage")[0])
