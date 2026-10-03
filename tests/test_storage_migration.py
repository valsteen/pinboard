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
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import _sync_directory, atomic_replace, create_immutable, resolve_durable_roots
from pinboard.adapters.files.legacy_storage import StorageLocation, observe_storage_location
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application.artifacts import ArtifactPublication, NewArtifact
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, JsonObject, JsonValue, complete_sqlite_state, initialize_store


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

    def test_published_migration_plans_report_their_effect_after_sync_failure(self) -> None:
        for route in ("migrate-schema", "migrate-work-root"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
                if route == "migrate-schema":
                    self.assertEqual(0, self.run_cli(project, "init")[0])
                    self.downgrade_schema(project / ".pinboard" / "state.sqlite3")
                    evidence = project / ".pinboard" / "migration"
                else:
                    self.initialize_legacy(project)
                    evidence = project / ".codex" / "pinboard-migration"
                plan_id = str(self.run_cli(project, route)[1]["plan_id"])
                plan_path = evidence / f"{plan_id}.json"

                def publish_then_fail(path: Path, content: bytes, expected_path: Path = plan_path) -> bool:
                    published = create_immutable(path, content)
                    if path == expected_path and published:
                        raise ImmutableFilePublishedError(
                            path, FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected sync failure")
                        )
                    return published

                owner = (
                    "pinboard.adapters.sqlite.schema_procedure"
                    if route == "migrate-schema"
                    else "pinboard.adapters.files.legacy_storage"
                )
                with patch(f"{owner}.create_immutable", side_effect=publish_then_fail):
                    code, failure = self.run_cli(project, route, "--apply", plan_id)
                self.assertEqual(11, code, failure)
                changed = failure["changed_surfaces"]
                assert isinstance(changed, list)
                self.assertIn("migration-evidence", changed)
                self.assertEqual("committed-effect", failure["status"])
                self.assertTrue(plan_path.is_file())
                self.assertEqual(0, self.run_cli(project, route, "--apply", plan_id)[0])

    def test_published_root_progress_reports_its_effect_after_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.initialize_legacy(project)
            plan_id = str(self.run_cli(project, "migrate-work-root")[1]["plan_id"])
            progress_path = project / ".codex" / "pinboard-migration" / f"{plan_id}.progress.json"

            publications = 0

            def stop_before_second_progress(path: Path, content: bytes) -> None:
                nonlocal publications
                publications += 1
                if publications == 2:
                    raise FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected sync failure")
                atomic_replace(path, content)

            with patch(
                "pinboard.adapters.files.legacy_storage.atomic_replace", side_effect=stop_before_second_progress
            ):
                self.assertEqual(11, self.run_cli(project, "migrate-work-root", "--apply", plan_id)[0])
            earlier_progress = progress_path.read_bytes()

            def publish_then_fail(path: Path, content: bytes) -> None:
                atomic_replace(path, content)
                if path == progress_path:
                    raise FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "injected sync failure")

            with patch("pinboard.adapters.files.legacy_storage.atomic_replace", side_effect=publish_then_fail):
                code, failure = self.run_cli(project, "migrate-work-root", "--apply", plan_id)
            self.assertEqual(11, code, failure)
            self.assertNotEqual(earlier_progress, progress_path.read_bytes())
            changed = failure["changed_surfaces"]
            assert isinstance(changed, list)
            self.assertIn("migration-evidence", changed)
            self.assertEqual("committed-effect", failure["status"])
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", plan_id)[0])

    def test_legacy_v6_root_uses_schema_migration_before_work_root_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            path = legacy / "state.sqlite3"
            store = SQLiteWorkStore(path)
            initialize_store(store, complete_sqlite_state())
            expected = store.validated_snapshot()
            self.downgrade_schema(path)
            v6_bytes = path.read_bytes()

            code, failure = self.run_cli(project, "migrate-work-root")
            self.assertEqual(12, code, failure)
            self.assertEqual("SCHEMA_UNSUPPORTED", failure["code"])
            schema_plan = self.run_cli(project, "--work-root", str(legacy), "migrate-schema")[1]
            schema_id = str(schema_plan["plan_id"])
            self.assertEqual(
                0, self.run_cli(project, "--work-root", str(legacy), "migrate-schema", "--apply", schema_id)[0]
            )
            root_plan = self.run_cli(project, "migrate-work-root")[1]
            root_id = str(root_plan["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", root_id)[0])
            self.assertEqual(0, self.run_cli(project, "status")[0])
            self.assertEqual(
                11, self.run_cli(project, "--work-root", str(legacy), "migrate-schema", "--reverse", schema_id)[0]
            )
            reverse_root = self.run_cli(project, "migrate-work-root", "--reverse", root_id)[1]
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", str(reverse_root["plan_id"]))[0])
            self.assertEqual(StorageLocation.LEGACY, observe_storage_location(project))
            self.assertTrue((legacy / "migration" / f"{schema_id}.json").is_file())
            reverse_schema = self.run_cli(
                project, "--work-root", str(legacy), "migrate-schema", "--reverse", schema_id
            )[1]
            reverse_plan = reverse_schema["plan"]
            assert isinstance(reverse_plan, dict)
            self.assertEqual(schema_id, reverse_plan["forward_plan_id"])
            self.assertEqual(
                0,
                self.run_cli(
                    project, "--work-root", str(legacy), "migrate-schema", "--apply", str(reverse_schema["plan_id"])
                )[0],
            )
            self.assertEqual(v6_bytes, path.read_bytes())
            self.assertEqual(
                0, self.run_cli(project, "--work-root", str(legacy), "migrate-schema", "--apply", schema_id)[0]
            )
            self.assertEqual(expected, SQLiteWorkStore(path).validated_snapshot())

    def run_cli(self, project: Path, *arguments: str) -> tuple[int, JsonObject]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(("--project-root", str(project), *arguments, "--json"))
        return code, json.loads(output.getvalue())

    def apply_migration(self, project: Path, *arguments: str) -> tuple[int, JsonObject]:
        code, preview = self.run_cli(project, *arguments)
        if code != 0:
            return code, preview
        return self.run_cli(project, *arguments, "--apply", str(preview["plan_id"]))

    def initialize_legacy(self, project: Path) -> Path:
        legacy = project / ".codex" / "pinboard"
        self.assertEqual(0, self.run_cli(project, "--work-root", str(legacy), "init")[0])
        return legacy

    def test_default_root_and_migration_state_matrix(self) -> None:
        for state in ("fresh", "legacy", "current", "alias", "conflict", "wrong-alias"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
                legacy = project / ".codex" / "pinboard"
                current = project / ".pinboard"
                if state == "legacy":
                    self.initialize_legacy(project)
                elif state in {"current", "alias", "conflict", "wrong-alias"}:
                    self.assertEqual(0, self.run_cli(project, "init")[0])
                    if state == "alias":
                        legacy.parent.mkdir()
                        legacy.symlink_to("../.pinboard", target_is_directory=True)
                    elif state == "conflict":
                        legacy.mkdir(parents=True)
                    elif state == "wrong-alias":
                        legacy.parent.mkdir()
                        legacy.symlink_to("../elsewhere", target_is_directory=True)
                self.assertEqual(
                    {
                        "fresh": StorageLocation.FRESH,
                        "legacy": StorageLocation.LEGACY,
                        "current": StorageLocation.CURRENT,
                        "alias": StorageLocation.ALIASED,
                        "conflict": StorageLocation.CONFLICT,
                        "wrong-alias": StorageLocation.CONFLICT,
                    }[state],
                    observe_storage_location(project),
                )
                code, result = self.apply_migration(project, "migrate-work-root")
                if state in {"legacy", "current", "alias"}:
                    self.assertEqual(0, code, result)
                    self.assertEqual("pinboard-work-root-migration/v2", result["schema"])
                    self.assertEqual(str(current), result["work_root"])
                    self.assertEqual(Path("../.pinboard"), legacy.readlink())
                else:
                    self.assertEqual(11, code, result)
                    self.assertEqual("rejected", result["status"])
                    self.assertFalse(result["state_changed"])

    def test_fresh_init_and_legacy_recovery_use_the_neutral_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            code, created = self.run_cli(project, "init")
            self.assertEqual(0, code, created)
            self.assertEqual(str(project / ".pinboard"), created["work_root"])
            self.assertFalse((project / ".codex").exists())
            self.assertIn("/.pinboard/", (project / ".git" / "info" / "exclude").read_text().splitlines())

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            self.initialize_legacy(project)
            for command in ("init", "status"):
                code, result = self.run_cli(project, command)
                self.assertEqual(11, code, result)
                self.assertEqual("WORK_ROOT_MIGRATION_REQUIRED", result["code"])
                observed = result["observed"]
                assert isinstance(observed, list)
                observations: dict[str, JsonValue] = {}
                for value in observed:
                    assert isinstance(value, dict)
                    observations[str(value["field"])] = value["value"]
                self.assertEqual("pinboard migrate-work-root, then --apply <plan-id>", observations["recovery_command"])
                self.assertFalse((project / ".pinboard").exists())

            contract_code, contract = self.run_cli(project, "tool-contract", "--operation", "migrate-work-root")
            self.assertEqual(0, contract_code, contract)
            self.assertEqual("read-only", contract["mutation_class"])
            self.assertEqual("safe-to-repeat", contract["retry_semantics"])

    def test_migration_preserves_populated_state_artifacts_and_unrelated_codex_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            (legacy.parent / "config.toml").write_text("unrelated\n")
            exclude = project / ".git" / "info" / "exclude"
            with exclude.open("ab") as stream:
                stream.write(b"/.codex/pinboard/\n")
            roots = resolve_durable_roots(project, legacy)
            publication = ArtifactRepository(roots).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "kept", 1, ".txt", b"immutable\n")
            )
            self.assertIsInstance(publication, ArtifactPublication)
            store = SQLiteWorkStore(roots.database_path)
            expect_success(store.accept_artifact_reference(legacy, publication.reference, SQLITE_NOW))
            before = store.validated_snapshot()
            database_bytes = roots.database_path.read_bytes()
            code, result = self.apply_migration(project, "migrate-work-root")
            self.assertEqual(0, code, result)
            self.assertEqual(
                ["repository-git-exclude", "migration-evidence", "work-root", "compatibility-alias"],
                result["changed_surfaces"],
            )
            current = project / ".pinboard"
            self.assertEqual(database_bytes, (current / "state.sqlite3").read_bytes())
            self.assertEqual(before, SQLiteWorkStore(current / "state.sqlite3").validated_snapshot())
            self.assertEqual(b"immutable\n", (current / publication.reference.selector).read_bytes())
            self.assertEqual("unrelated\n", (legacy.parent / "config.toml").read_text())
            for path in (".pinboard/state.sqlite3", ".codex/pinboard"):
                self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", path], cwd=project).returncode)
            exclude_lines = exclude.read_text().splitlines()
            self.assertIn("/.codex/pinboard/", exclude_lines)
            self.assertIn("/.pinboard/", exclude_lines)

    def test_partial_move_and_alias_failures_report_exact_effects_and_repair(self) -> None:
        for operation, expected in (
            ("rename", ["repository-git-exclude"]),
            ("symlink_to", ["repository-git-exclude", "work-root"]),
        ):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
                legacy = self.initialize_legacy(project)
                before = (legacy / "state.sqlite3").read_bytes()
                preview = self.run_cli(project, "migrate-work-root")[1]
                with patch.object(Path, operation, side_effect=PermissionError("injected failure")):
                    code, failure = self.run_cli(project, "migrate-work-root", "--apply", str(preview["plan_id"]))
                self.assertEqual(11, code, failure)
                changed = failure["changed_surfaces"]
                assert isinstance(changed, list)
                self.assertTrue(set(expected) <= set(changed))
                self.assertEqual(("committed-effect", "retry-same-input"), (failure["status"], failure["retry"]))
                self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", str(preview["plan_id"]))[0])
                self.assertEqual(before, (project / ".pinboard" / "state.sqlite3").read_bytes())

    def test_target_only_alias_failure_leaves_no_alias_effect_or_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])

            preview = self.run_cli(project, "migrate-work-root")[1]
            with patch.object(Path, "symlink_to", side_effect=PermissionError("injected failure")):
                code, failure = self.run_cli(project, "migrate-work-root", "--apply", str(preview["plan_id"]))

            self.assertEqual(11, code, failure)
            changed = failure["changed_surfaces"]
            assert isinstance(changed, list)
            self.assertIn("migration-evidence", changed)
            self.assertEqual(("committed-effect", "retry-same-input"), (failure["status"], failure["retry"]))
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", str(preview["plan_id"]))[0])

    def test_exact_alias_is_canonical_and_arbitrary_symlink_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, external = root / "project", root / "external"
            project.mkdir()
            external.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            legacy = project / ".codex" / "pinboard"
            legacy.parent.mkdir()
            legacy.symlink_to("../.pinboard", target_is_directory=True)
            self.assertEqual(project / ".pinboard", resolve_durable_roots(project, legacy).work_root)
            linked_parent = project / "linked-parent"
            linked_parent.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(Exception, "DIRECTORY_INVALID"):
                resolve_durable_roots(project, linked_parent / "work")

    def test_repository_alias_preserves_explicit_legacy_root_through_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real_parent = root / "real-parent"
            spelled_parent = root / "spelled-parent"
            real_parent.mkdir()
            spelled_parent.symlink_to(real_parent, target_is_directory=True)
            project = real_parent / "project"
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            spelled_project = spelled_parent / "project"
            spelled_legacy = spelled_project / ".codex" / "pinboard"

            init_code, init_result = self.run_cli(
                spelled_project,
                "--work-root",
                str(spelled_legacy),
                "init",
            )
            self.assertEqual(0, init_code, init_result)
            migration_code, migration = self.apply_migration(spelled_project, "migrate-work-root")
            self.assertEqual(0, migration_code, migration)

            current = project / ".pinboard"
            legacy = project / ".codex" / "pinboard"
            self.assertTrue((current / "state.sqlite3").is_file())
            self.assertEqual(Path("../.pinboard"), legacy.readlink())
            for path in (".pinboard/state.sqlite3", ".codex/pinboard"):
                self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", path], cwd=project).returncode)
            reopen_code, reopen = self.run_cli(spelled_project, "status")
            self.assertEqual(0, reopen_code, reopen)

    def test_previews_are_read_only_and_stale_plans_reject_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            database = legacy / "state.sqlite3"
            self.downgrade_schema(database)
            exclude = project / ".git" / "info" / "exclude"
            before_exclude = exclude.read_bytes()
            before_database = database.read_bytes()
            schema_plan = self.run_cli(project, "--work-root", str(legacy), "migrate-schema")[1]
            root_code, root_failure = self.run_cli(project, "migrate-work-root")
            self.assertEqual(12, root_code, root_failure)
            self.assertEqual(before_exclude, exclude.read_bytes())
            self.assertEqual(before_database, database.read_bytes())
            self.assertFalse((legacy / "migration").exists())
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            code, stale = self.run_cli(
                project, "--work-root", str(legacy), "migrate-schema", "--apply", str(schema_plan["plan_id"])
            )
            self.assertEqual(11, code, stale)
            self.assertEqual([], stale["changed_surfaces"])
            self.assertFalse((legacy / "migration").exists())

    def test_root_exclusion_interruption_resumes_original_plan_without_repreview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.initialize_legacy(project)
            preview = self.run_cli(project, "migrate-work-root")[1]
            plan_id = str(preview["plan_id"])
            with patch(
                "pinboard.adapters.files.legacy_storage.create_immutable",
                side_effect=PermissionError("plan publication denied"),
            ):
                code, failure = self.run_cli(project, "migrate-work-root", "--apply", plan_id)
            self.assertEqual(11, code, failure)
            self.assertEqual(["repository-git-exclude"], failure["changed_surfaces"])
            self.assertFalse((project / ".codex" / "pinboard-migration" / f"{plan_id}.json").exists())
            self.assertEqual(StorageLocation.LEGACY, observe_storage_location(project))
            resumed_code, resumed = self.run_cli(project, "migrate-work-root", "--apply", plan_id)
            self.assertEqual(0, resumed_code, resumed)
            self.assertEqual(StorageLocation.ALIASED, observe_storage_location(project))
            self.assertTrue((project / ".codex" / "pinboard-migration" / f"{plan_id}.backup").is_dir())

    def test_root_apply_requires_verifiable_repository_git_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            self.initialize_legacy(project)
            plan_id = str(self.run_cli(project, "migrate-work-root")[1]["plan_id"])

            code, failure = self.run_cli(project, "migrate-work-root", "--apply", plan_id)

            self.assertEqual(11, code, failure)
            self.assertEqual("WORK_ROOT_MIGRATION_INVALID", failure["code"])
            self.assertEqual([], failure["changed_surfaces"])
            self.assertEqual("correct-input", failure["retry"])
            self.assertIn("PROJECT_GIT_ROOT_UNAVAILABLE", str(failure["message"]))
            self.assertIn(str(project / ".git" / "info" / "exclude"), str(failure["message"]))
            self.assertFalse((project / ".codex" / "pinboard-migration").exists())
            self.assertEqual(StorageLocation.LEGACY, observe_storage_location(project))

    def test_stale_root_plan_rejects_before_git_or_authority_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            before_exclude = (project / ".git" / "info" / "exclude").read_bytes()
            plan = self.run_cli(project, "migrate-work-root")[1]
            (legacy / "operator-note.txt").write_text("later local change\n")
            code, rejected = self.run_cli(project, "migrate-work-root", "--apply", str(plan["plan_id"]))
            self.assertEqual(11, code, rejected)
            self.assertEqual([], rejected["changed_surfaces"])
            self.assertEqual(before_exclude, (project / ".git" / "info" / "exclude").read_bytes())
            self.assertEqual(StorageLocation.LEGACY, observe_storage_location(project))
            self.assertFalse((project / ".codex" / "pinboard-migration").exists())

    def test_root_reverse_refuses_later_authoritative_write_but_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            forward = self.run_cli(project, "migrate-work-root")[1]
            forward_id = str(forward["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", forward_id)[0])
            backup = project / ".codex" / "pinboard-migration" / f"{forward_id}.backup"
            self.assertTrue(backup.is_dir())
            database = project / ".pinboard" / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            code, refused = self.run_cli(project, "migrate-work-root", "--reverse", forward_id)
            self.assertEqual(11, code, refused)
            message = refused["message"]
            assert isinstance(message, str)
            self.assertIn("changed after relocation", message)
            self.assertEqual(StorageLocation.ALIASED, observe_storage_location(project))
            self.assertTrue(backup.is_dir())
            self.assertTrue((legacy / "state.sqlite3").is_file())

    def test_current_alias_repair_has_no_data_backup_and_aliased_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            forward = self.run_cli(project, "migrate-work-root")[1]
            forward_id = str(forward["plan_id"])
            forward_plan = forward["plan"]
            assert isinstance(forward_plan, dict)
            self.assertEqual("current", forward_plan["starting_location"])
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", forward_id)[0])
            sidecar = project / ".codex" / "pinboard-migration"
            self.assertFalse((sidecar / f"{forward_id}.backup").exists())
            aliased = self.run_cli(project, "migrate-work-root")[1]
            noop_plan = aliased["plan"]
            assert isinstance(noop_plan, dict)
            self.assertEqual("unchanged", noop_plan["kind"])
            applied = self.run_cli(project, "migrate-work-root", "--apply", str(aliased["plan_id"]))[1]
            self.assertEqual("unchanged", applied["status"])
            self.assertEqual([], applied["changed_surfaces"])
            self.assertEqual(11, self.run_cli(project, "migrate-work-root", "--reverse", str(aliased["plan_id"]))[0])
            before_exclude = (project / ".git" / "info" / "exclude").read_bytes()
            before_current = (project / ".pinboard" / "state.sqlite3").read_bytes()
            before_sidecar = sorted(path.name for path in sidecar.iterdir())
            reverse = self.run_cli(project, "migrate-work-root", "--reverse", forward_id)[1]
            self.assertEqual(before_exclude, (project / ".git" / "info" / "exclude").read_bytes())
            self.assertEqual(before_current, (project / ".pinboard" / "state.sqlite3").read_bytes())
            self.assertEqual(before_sidecar, sorted(path.name for path in sidecar.iterdir()))
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", str(reverse["plan_id"]))[0])
            self.assertEqual(StorageLocation.CURRENT, observe_storage_location(project))
            self.assertTrue((project / ".pinboard" / "state.sqlite3").is_file())

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

    def test_root_move_resumes_after_verified_backup_before_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            legacy = self.initialize_legacy(project)
            forward = self.run_cli(project, "migrate-work-root")[1]
            plan_id = str(forward["plan_id"])
            original_rename = Path.rename

            def fail_root_rename(source: Path, destination: Path) -> Path:
                if source == legacy:
                    raise PermissionError("root switch denied")
                return original_rename(source, destination)

            with patch.object(Path, "rename", fail_root_rename):
                code, failure = self.run_cli(project, "migrate-work-root", "--apply", plan_id)
            self.assertEqual(11, code, failure)
            self.assertTrue((project / ".codex" / "pinboard-migration" / f"{plan_id}.backup").is_dir())
            self.assertEqual(StorageLocation.LEGACY, observe_storage_location(project))
            self.assertEqual(0, self.run_cli(project, "migrate-work-root", "--apply", plan_id)[0])
            self.assertEqual(StorageLocation.ALIASED, observe_storage_location(project))


if __name__ == "__main__":
    unittest.main()
