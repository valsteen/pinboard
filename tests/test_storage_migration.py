import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import (
    ArtifactError,
    ArtifactErrorCode,
    FileIOError,
    FileIOErrorCode,
    ImmutableFilePublishedError,
)
from pinboard.adapters.files.file_io import _sync_directory, create_immutable, resolve_durable_roots
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import ArtifactPublication, NewArtifact
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, JsonObject, complete_sqlite_state, initialize_store


class StorageMigrationTests(unittest.TestCase):
    def downgrade_schema(self, database_path: Path) -> None:
        with contextlib.closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("DROP INDEX checkpoint_history_by_subject")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v7")
            connection.execute(
                """CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 6),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT"""
            )
            connection.execute(
                """INSERT INTO project_meta
                   SELECT singleton, application, 6, revision, host_epoch, created_at, updated_at
                   FROM project_meta_v7"""
            )
            connection.execute("DROP TABLE project_meta_v7")

    def test_populated_v6_ledger_migrates_explicitly_and_failed_upgrade_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            path = resolve_durable_roots(project).database_path
            store = SQLiteWorkStore(path)
            initialize_store(store, complete_sqlite_state())
            expected = store.validated_snapshot()
            self.downgrade_schema(path)

            before_read = path.read_bytes()
            status_code, status = self.run_cli(project, "status")
            self.assertEqual(12, status_code)
            self.assertEqual("SCHEMA_UNSUPPORTED", status["code"])
            self.assertIn("pinboard migrate-schema", str(status["message"]))
            self.assertEqual(before_read, path.read_bytes())

            preview_code, preview = self.run_cli(project, "migrate-schema")
            self.assertEqual(0, preview_code, preview)
            self.assertEqual("planned", preview["status"])
            self.assertEqual(before_read, path.read_bytes())
            plan_id = str(preview["plan_id"])
            with (
                patch(
                    "pinboard.adapters.sqlite.schema_procedure.migrate_v6_database",
                    side_effect=StorageError(StorageErrorCode.INVALID_STATE, "forced verification failure"),
                ),
            ):
                code, failed = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(11, code, failed)
            changed = failed["changed_surfaces"]
            assert isinstance(changed, list)
            self.assertIn("migration-evidence", changed)
            self.assertEqual(before_read, (path.parent / "migration" / f"{plan_id}.v6").read_bytes())
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertEqual(6, connection.execute("SELECT schema_version FROM project_meta").fetchone()[0])
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name = 'checkpoint_history_by_subject'"
                    ).fetchone()
                )
            reverse_code, reverse_failure = self.run_cli(project, "migrate-schema", "--reverse", plan_id)
            self.assertEqual(11, reverse_code, reverse_failure)
            self.assertEqual([], reverse_failure["changed_surfaces"])

            migration_code, migration = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(0, migration_code, migration)
            self.assertEqual("migrated", migration["status"])
            self.assertEqual("sqlite-v7", migration["authority"])
            self.assertEqual(expected, SQLiteWorkStore(path).validated_snapshot())
            repeated_code, repeated = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(0, repeated_code, repeated)
            self.assertEqual("unchanged", repeated["status"])

    def test_schema_apply_reports_committed_effect_without_a_second_database_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            path = resolve_durable_roots(project).database_path
            self.downgrade_schema(path)
            plan_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            with patch(
                "pinboard.cli.schema_migration.inspect_schema_migration",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "post-commit read denied"),
            ):
                code, result = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(0, code, result)
            self.assertEqual("committed", result["effect"])
            changed_surfaces = result["changed_surfaces"]
            assert isinstance(changed_surfaces, list)
            self.assertIn("ledger", changed_surfaces)
            with sqlite3.connect(path) as connection:
                self.assertEqual(7, connection.execute("SELECT schema_version FROM project_meta").fetchone()[0])

    def test_schema_reverse_reports_restored_ledger_after_directory_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            database = project / ".pinboard" / "state.sqlite3"
            self.downgrade_schema(database)
            original = database.read_bytes()
            forward_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", forward_id)[0])
            reverse_id = str(self.run_cli(project, "migrate-schema", "--reverse", forward_id)[1]["plan_id"])

            def fail_database_directory_sync(path: Path) -> None:
                if path == database.parent:
                    raise FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected sync failure")
                _sync_directory(path)

            with patch(
                "pinboard.adapters.files.file_io._sync_directory",
                side_effect=fail_database_directory_sync,
            ):
                code, failure = self.run_cli(project, "migrate-schema", "--apply", reverse_id)
            self.assertEqual(11, code, failure)
            changed = failure["changed_surfaces"]
            assert isinstance(changed, list)
            self.assertIn("ledger", changed)
            self.assertEqual("committed-effect", failure["status"])
            self.assertEqual(original, database.read_bytes())
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", reverse_id)[0])

    def run_cli(self, project: Path, *arguments: str) -> tuple[int, JsonObject]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(("--project-root", str(project), *arguments, "--json"))
        return code, json.loads(output.getvalue())

    def test_schema_reverse_refuses_later_v7_write_and_retains_exact_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            database = project / ".pinboard" / "state.sqlite3"
            self.downgrade_schema(database)
            before = database.read_bytes()
            forward = self.run_cli(project, "migrate-schema")[1]
            forward_id = str(forward["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", forward_id)[0])
            backup = project / ".pinboard" / "migration" / f"{forward_id}.v6"
            self.assertEqual(before, backup.read_bytes())
            migrated_bytes = database.read_bytes()
            reverse = self.run_cli(project, "migrate-schema", "--reverse", forward_id)[1]
            self.assertEqual("planned", reverse["status"])
            self.assertEqual(migrated_bytes, database.read_bytes())
            self.assertEqual(before, backup.read_bytes())
            self.assertFalse((project / ".pinboard" / "migration" / f"{reverse['plan_id']}.json").exists())
            self.assertEqual(0, self.run_cli(project, "status")[0])
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            code, refused = self.run_cli(project, "migrate-schema", "--apply", str(reverse["plan_id"]))
            self.assertEqual(11, code, refused)
            self.assertEqual([], refused["changed_surfaces"])
            self.assertEqual(before, backup.read_bytes())
            self.assertEqual(0, self.run_cli(project, "status")[0])

    def test_published_schema_plan_reports_its_effect_after_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            self.downgrade_schema(project / ".pinboard" / "state.sqlite3")
            plan_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            plan_path = project / ".pinboard" / "migration" / f"{plan_id}.json"

            def publish_then_fail(path: Path, content: bytes) -> bool:
                published = create_immutable(path, content)
                if path == plan_path and published:
                    raise ImmutableFilePublishedError(
                        path, FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected sync failure")
                    )
                return published

            with patch("pinboard.adapters.sqlite.schema_procedure.create_immutable", side_effect=publish_then_fail):
                code, failure = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(11, code, failure)
            changed = failure["changed_surfaces"]
            assert isinstance(changed, list)
            self.assertIn("migration-evidence", changed)
            self.assertEqual("committed-effect", failure["status"])
            self.assertTrue(plan_path.is_file())
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", plan_id)[0])

    def test_explicit_root_schema_round_trip_preserves_populated_state_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            selected = project / ".codex" / "pinboard"
            selected.parent.mkdir()
            args = ("--work-root", str(selected))
            self.assertEqual(0, self.run_cli(project, *args, "init")[0])
            roots = resolve_durable_roots(project, selected)
            store = SQLiteWorkStore(roots.database_path)
            initialize_store(store, complete_sqlite_state())
            expected = store.validated_snapshot()
            self.downgrade_schema(roots.database_path)
            original = roots.database_path.read_bytes()
            forward_id = str(self.run_cli(project, *args, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", forward_id)[0])
            self.assertEqual(expected, SQLiteWorkStore(roots.database_path).validated_snapshot())
            reverse_id = str(self.run_cli(project, *args, "migrate-schema", "--reverse", forward_id)[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", reverse_id)[0])
            self.assertEqual(original, roots.database_path.read_bytes())
            self.assertEqual(original, (selected / "migration" / f"{forward_id}.v6").read_bytes())
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", forward_id)[0])
            self.assertEqual(expected, SQLiteWorkStore(roots.database_path).validated_snapshot())
            self.assertFalse((project / ".pinboard").exists())

    def test_schema_preview_is_read_only_and_stale_apply_rejects_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            database = project / ".pinboard" / "state.sqlite3"
            self.downgrade_schema(database)
            exclude = project / ".git" / "info" / "exclude"
            before_exclude = exclude.read_bytes()
            before_database = database.read_bytes()
            schema_plan = self.run_cli(project, "migrate-schema")[1]
            self.assertEqual(before_exclude, exclude.read_bytes())
            self.assertEqual(before_database, database.read_bytes())
            evidence = database.parent / "migration"
            self.assertFalse(evidence.exists())
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            changed_database = database.read_bytes()
            code, stale = self.run_cli(project, "migrate-schema", "--apply", str(schema_plan["plan_id"]))
            self.assertEqual(11, code, stale)
            self.assertEqual([], stale["changed_surfaces"])
            self.assertEqual(before_exclude, exclude.read_bytes())
            self.assertEqual(changed_database, database.read_bytes())
            self.assertFalse(evidence.exists())

    def test_default_init_is_idempotent_and_does_not_inspect_other_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            other = project / ".codex" / "pinboard"
            other.mkdir(parents=True)
            retained = other / "state.sqlite3"
            retained.write_bytes(b"unrelated retained state")
            for _ in range(2):
                code, created = self.run_cli(project, "init")
                self.assertEqual(0, code, created)
                self.assertEqual(str(project / ".pinboard"), created["work_root"])
            exclude = project / ".git" / "info" / "exclude"
            self.assertEqual(1, exclude.read_text().splitlines().count("/.pinboard/"))
            self.assertEqual(b"unrelated retained state", retained.read_bytes())
            self.assertFalse(other.is_symlink())

    def test_real_explicit_roots_initialize_reopen_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            project = base / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            (project / ".codex").mkdir()
            for selected in (project / ".codex" / "work", base / "external-work"):
                with self.subTest(selected=selected):
                    args = ("--work-root", str(selected))
                    code, created = self.run_cli(project, *args, "init")
                    self.assertEqual(0, code, created)
                    self.assertEqual(str(selected), created["work_root"])
                    roots = resolve_durable_roots(project, selected)
                    publication = ArtifactRepository(roots).publish(
                        NewArtifact(work_models.ArtifactKind.EVIDENCE, "kept", 1, ".txt", b"immutable")
                    )
                    assert isinstance(publication, ArtifactPublication)
                    expect_success(
                        SQLiteWorkStore(roots.database_path).accept_artifact_reference(
                            selected, publication.reference, SQLITE_NOW
                        )
                    )
                    fresh = SQLiteWorkStore(roots.database_path).validated_snapshot()
                    self.assertEqual(b"immutable", ArtifactRepository(roots).read(fresh.artifact_references[0]))
                    self.assertEqual(0, self.run_cli(project, *args, "status")[0])
            self.assertFalse((project / ".pinboard").exists())

    def test_generic_alias_reads_exact_bytes_and_computed_publication_requires_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            current = resolve_durable_roots(project)
            publication = ArtifactRepository(current).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "kept", 1, ".txt", b"immutable")
            )
            assert isinstance(publication, ArtifactPublication)
            expect_success(
                SQLiteWorkStore(current.database_path).accept_artifact_reference(
                    current.work_root, publication.reference, SQLITE_NOW
                )
            )
            reference = SQLiteWorkStore(current.database_path).validated_snapshot().artifact_references[0]
            alias = project / ".codex" / "pinboard"
            alias.parent.mkdir()
            alias.symlink_to("../.pinboard", target_is_directory=True)
            selected = resolve_durable_roots(project, alias)
            self.assertEqual(alias, selected.work_root)
            self.assertEqual(0, self.run_cli(project, "--work-root", str(alias), "status")[0])
            self.assertEqual(b"immutable", ArtifactRepository(selected).read(reference))
            with self.assertRaises(ArtifactError) as raised:
                ArtifactRepository(selected).publish(
                    NewArtifact(work_models.ArtifactKind.EVIDENCE, "new", 1, ".txt", b"new")
                )
            self.assertEqual(ArtifactErrorCode.STORAGE_IO_ERROR, raised.exception.code)
            self.assertEqual(b"immutable", ArtifactRepository(current).read(reference))
            self.assertFalse((current.artifacts_root / "evidence" / "new").exists())
            linked_parent = project / "linked-parent"
            linked_parent.symlink_to(alias.parent, target_is_directory=True)
            with self.assertRaises(FileIOError):
                resolve_durable_roots(project, linked_parent / "work")

    def test_retired_root_migration_rejects_all_forms_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            for options in ((), ("--apply", "plan"), ("--reverse", "plan")):
                with self.subTest(options=options):
                    code, failure = self.run_cli(project, "migrate-work-root", *options)
                    self.assertEqual(2, code, failure)
                    self.assertFalse(failure["state_changed"])
                    self.assertEqual([], failure["changed_surfaces"])
            code, failure = self.run_cli(project, "tool-contract", "--operation", "migrate-work-root")
            self.assertEqual(11, code, failure)
            self.assertEqual([], failure["changed_surfaces"])
            self.assertEqual([], list(project.iterdir()))


if __name__ == "__main__":
    unittest.main()
