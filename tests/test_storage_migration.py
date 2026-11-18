import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import (
    FileIOError,
    FileIOErrorCode,
    ImmutableFilePublishedError,
)
from pinboard.adapters.files.file_io import _sync_directory, create_immutable, resolve_durable_roots
from pinboard.adapters.sqlite import database as sqlite_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import schema_unblockers_v12
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactPublication, NewArtifact
from pinboard.application.work_briefs import canonical_work_brief_bytes
from pinboard.cli.entrypoint import main
from pinboard.domain import work_models
from pinboard.domain.identifiers import TaskId, WorkItemId
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.domain_support import expect_success
from tests.native_support import call_advertised_tool
from tests.sqlite_support import advanced_v13_copy, downgrade_to_v12, downgrade_to_v14
from tests.support import SQLITE_NOW, JsonObject, complete_sqlite_state, initialize_store, test_definition
from tests.work_brief_support import work_a_brief


class StorageMigrationTests(unittest.TestCase):
    def predecessor_rows(self, path: Path) -> tuple[str, ...]:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            facts: list[str] = []
            for (table,) in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"):
                if table in {
                    "project_meta",
                    "artifact_ownership",
                    "checkpoint_acceptances",
                    "attempt_decision_batches",
                    "work_item_unblockers",
                    "released_item_holds",
                }:
                    continue
                columns = tuple(
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                    if (table, row[1])
                    not in {
                        ("item_dependencies", "checkpoint_id"),
                        ("proposals", "relation_checkpoint_id"),
                    }
                )
                selected = ", ".join(f'"{column}"' for column in columns)
                facts.extend(
                    repr((table, tuple(row)))
                    for row in connection.execute(f'SELECT {selected} FROM "{table}"')
                    # v12 and earlier count the retired deferred state; an empty count is no predecessor fact.
                    if (table, tuple(row)) != ("work_item_state_counts", ("deferred", 0))
                )
            return tuple(sorted(facts))

    def downgrade_v11(self, path: Path) -> None:
        downgrade_to_v12(path)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE item_off_roadmap_episodes")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v12")
            statement = next(
                sql for _, name, _, sql in sqlite_database._build_v11_schema_signature() if name == "project_meta"
            )
            assert statement is not None
            connection.execute(statement)
            connection.execute(
                "INSERT INTO project_meta SELECT singleton, application, 11, revision, host_epoch, created_at, updated_at FROM project_meta_v12"
            )
            connection.execute("DROP TABLE project_meta_v12")
            sqlite_database._verify_v11_schema(connection)

    def downgrade_v9(self, path: Path) -> None:
        self.downgrade_v11(path)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE attempt_decision_batches")
            connection.execute("DROP TABLE checkpoint_acceptances")
            connection.execute("ALTER TABLE item_dependencies DROP COLUMN checkpoint_id")
            connection.execute("ALTER TABLE proposals DROP COLUMN relation_checkpoint_id")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v10")
            statement = next(
                sql for _, name, _, sql in sqlite_database._build_v9_schema_signature() if name == "project_meta"
            )
            assert statement is not None
            connection.execute(statement)
            connection.execute(
                "INSERT INTO project_meta SELECT singleton, application, 9, revision, host_epoch, created_at, updated_at FROM project_meta_v10"
            )
            connection.execute("DROP TABLE project_meta_v10")
            sqlite_database._verify_v9_schema(connection)

    def initialize_v8(self, project: Path) -> Path:
        subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
        self.assertEqual(0, self.run_cli(project, "init")[0])
        path = resolve_durable_roots(project).database_path
        self.downgrade_v8(path)
        return path

    def downgrade_v8(self, path: Path) -> None:
        self.downgrade_v9(path)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE artifact_ownership")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v9")
            connection.execute("""CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 8),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""")
            connection.execute(
                "INSERT INTO project_meta SELECT singleton, application, 8, revision, host_epoch, created_at, updated_at FROM project_meta_v9"
            )
            connection.execute("DROP TABLE project_meta_v9")

    def test_v8_ownership_upgrade_is_bound_recoverable_and_reversible_until_a_new_write(self) -> None:  # noqa: PLR0915 - one retained recovery journey
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            path = self.initialize_v8(project)
            original = path.read_bytes()
            code, preview = self.run_cli(project, "migrate-schema")
            self.assertEqual(0, code, preview)
            self.assertEqual(original, path.read_bytes())
            plan_id = str(preview["plan_id"])
            with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            original = path.read_bytes()
            code, stale = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(11, code, stale)
            self.assertEqual([], stale["changed_surfaces"])
            self.assertEqual(original, path.read_bytes())
            plan_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            with patch(
                "pinboard.adapters.sqlite.migration.schema_procedure.migrate_v8_database",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "controlled interruption"),
            ):
                code, failed = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(11, code, failed)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(original, (path.parent / "migration" / f"{plan_id}.v8").read_bytes())
            verify = sqlite_database._verify_v9_schema
            interrupted: list[bool] = []

            def reject_live_postimage(connection: sqlite3.Connection) -> None:
                verify(connection)
                if connection.execute("PRAGMA database_list").fetchone()[2] == str(path):
                    interrupted.append(True)
                    raise StorageError(StorageErrorCode.INVALID_STATE, "controlled failure before authority commit")

            with patch.object(sqlite_database, "_verify_v9_schema", side_effect=reject_live_postimage):
                self.assertEqual(11, self.run_cli(project, "migrate-schema", "--apply", plan_id)[0])
            self.assertEqual([True], interrupted)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", plan_id)[0])
            reverse = str(self.run_cli(project, "migrate-schema", "--reverse", plan_id)[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", reverse)[0])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", plan_id)[0])
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertEqual(9, connection.execute("SELECT schema_version FROM project_meta").fetchone()[0])
            current = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", current)[0])
            with contextlib.closing(sqlite3.connect(path)) as connection:
                sqlite_database._verify_v10_schema(connection)
            for _step in ("v10-to-v11", "v11-to-v12", "v12-to-v13", "v13-to-v14", "v14-to-v15"):
                current = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
                self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", current)[0])
            self.assertEqual(15, SQLiteWorkStore(path).validated_snapshot().lifecycle.project.schema_version)
            roots = resolve_durable_roots(project)
            publication = ArtifactRepository(roots).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "later", 1, ".txt", b"later")
            )
            assert isinstance(publication, ArtifactPublication)
            expect_success(
                SQLiteWorkStore(path).accept_artifact_reference(
                    roots.work_root, publication.reference, SQLITE_NOW, ownership=None
                )
            )
            later = path.read_bytes()
            self.assertEqual(11, self.run_cli(project, "migrate-schema", "--reverse", plan_id)[0])
            self.assertEqual(later, path.read_bytes())

    def downgrade_schema(self, database_path: Path) -> None:
        self.downgrade_v9(database_path)
        with contextlib.closing(sqlite3.connect(database_path)) as connection, connection:
            for table in (
                "artifact_ownership",
                "initiative_assessments",
                "initiative_decisions",
                "initiative_memberships",
                "initiative_revisions",
                "initiative_contexts",
                "initiatives",
            ):
                connection.execute(f"DROP TABLE {table}")
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

    def v12_ledger(self, project: Path) -> Path:
        subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
        self.assertEqual(0, self.run_cli(project, "init")[0])
        path = resolve_durable_roots(project).database_path
        initialize_store(SQLiteWorkStore(path), complete_sqlite_state())
        downgrade_to_v12(path)
        return path

    def hold_v12(self, path: Path, item_id: str, state: str) -> None:
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            before = connection.execute("SELECT state FROM work_items WHERE item_id = ?", (item_id,)).fetchone()[0]
            connection.execute("UPDATE work_items SET state = ? WHERE item_id = ?", (state, item_id))
            connection.execute(
                "UPDATE work_item_state_counts SET item_count = item_count - 1 WHERE state = ?", (before,)
            )
            connection.execute(
                "UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = ?", (state,)
            )

    def write_labels(self, path: Path, source: str, labels: Sequence[object]) -> Path:
        content = json.dumps({"schema": "pinboard-hold-labels/v1", "source_sha256": source, "labels": labels})
        label_file = path.parent.parent / f"labels-{hashlib.sha256(content.encode()).hexdigest()[:12]}.json"
        label_file.write_text(content)
        return label_file

    def test_v12_step_refuses_unlabelled_holds_applies_exact_labels_and_reverses_until_a_new_write(self) -> None:  # noqa: PLR0915 - one labelled migration, resume and reversal journey
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            path = self.v12_ledger(project)
            self.hold_v12(path, "work-c", "blocked")
            self.hold_v12(path, "zz-proposal-a", "deferred")
            original = path.read_bytes()
            source = hashlib.sha256(original).hexdigest()
            code, refused = self.run_cli(project, "migrate-schema")
            self.assertNotEqual(0, code)
            message = str(refused["message"])
            self.assertIn("work-c (blocked)", message)
            self.assertIn("zz-proposal-a (deferred)", message)
            self.assertIn("--labels", message)
            self.assertEqual(original, path.read_bytes())
            question: JsonObject = {"kind": "human-decision", "question": "Should work C wait for the new survey?"}
            condition: JsonObject = {"kind": "outside-condition", "condition": "The vendor publishes its v2 API."}
            for labels, refusal in (
                ([{"item_id": "work-c", "attempt_id": None, "unblocker": question}], "zz-proposal-a (deferred)"),
                (
                    [
                        {"item_id": "work-c", "attempt_id": None, "unblocker": {"kind": "items", "items": ["work-a"]}},
                        {"item_id": "zz-proposal-a", "attempt_id": None, "unblocker": condition},
                    ],
                    "not its accepted dependencies",
                ),
                (
                    [
                        {"item_id": "work-c", "attempt_id": None, "unblocker": question},
                        {"item_id": "zz-proposal-a", "attempt_id": None, "unblocker": question},
                    ],
                    "outside condition",
                ),
            ):
                with self.subTest(refusal=refusal):
                    label_file = self.write_labels(path, source, labels)
                    code, rejected = self.run_cli(project, "migrate-schema", "--labels", str(label_file))
                    self.assertNotEqual(0, code)
                    self.assertIn(refusal, str(rejected["message"]))
            complete = [
                {"item_id": "work-c", "attempt_id": None, "unblocker": question},
                {"item_id": "zz-proposal-a", "attempt_id": None, "unblocker": condition},
            ]
            stale = self.write_labels(path, "0" * 64, complete)
            code, rejected = self.run_cli(project, "migrate-schema", "--labels", str(stale))
            self.assertNotEqual(0, code)
            self.assertIn("another ledger state", str(rejected["message"]))
            label_file = self.write_labels(path, source, complete)
            code, planned = self.run_cli(project, "migrate-schema", "--labels", str(label_file))
            self.assertEqual(0, code, planned)
            plan = planned["plan"]
            assert isinstance(plan, dict)
            self.assertEqual("unblockers-advance", plan["kind"])
            self.assertEqual(hashlib.sha256(label_file.read_bytes()).hexdigest(), plan["labels_sha256"])
            plan_id = str(planned["plan_id"])
            other = self.write_labels(path, source, list(reversed(complete)))
            code, rejected = self.run_cli(project, "migrate-schema", "--apply", plan_id, "--labels", str(other))
            self.assertNotEqual(0, code)
            self.assertIn("request a new preview", str(rejected["message"]))
            self.assertEqual(original, path.read_bytes())
            code, migrated = self.run_cli(project, "migrate-schema", "--apply", plan_id, "--labels", str(label_file))
            self.assertEqual(0, code, migrated)
            self.assertEqual(("migrated", "sqlite-v13"), (migrated["status"], migrated["authority"]))
            evidence = path.parent / "migration"
            self.assertEqual(original, (evidence / f"{plan_id}.v12").read_bytes())
            self.assertEqual(label_file.read_bytes(), (evidence / f"{plan_id}.labels.json").read_bytes())
            reloaded = SQLiteWorkStore(advanced_v13_copy(path)).validated_snapshot()
            self.assertEqual(
                {
                    WorkItemId("work-c"): work_models.WaitingForHumanDecision("Should work C wait for the new survey?"),
                    WorkItemId("zz-proposal-a"): work_models.WaitingForOutsideCondition(
                        "The vendor publishes its v2 API."
                    ),
                },
                {value.item_id: value.unblocker for value in reloaded.lifecycle.unblockers},
            )
            self.assertEqual(
                stored_state.StoredWorkItemState.BLOCKED,
                next(value.state for value in reloaded.lifecycle.work_items if value.item_id == "zz-proposal-a"),
            )
            resumed = self.run_cli(project, "migrate-schema", "--apply", plan_id, "--labels", str(label_file))[1]
            self.assertEqual("unchanged", resumed["status"])
            reverse = self.run_cli(project, "migrate-schema", "--reverse", plan_id)[1]
            reversed_result = self.run_cli(project, "migrate-schema", "--apply", str(reverse["plan_id"]))[1]
            self.assertEqual(("reversed", "sqlite-v12"), (reversed_result["status"], reversed_result["authority"]))
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(
                0, self.run_cli(project, "migrate-schema", "--apply", plan_id, "--labels", str(label_file))[0]
            )
            with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            code, later = self.run_cli(project, "migrate-schema", "--reverse", plan_id)
            self.assertNotEqual(0, code)
            self.assertIn("changed after schema migration", str(later["message"]))

    def test_v12_step_releases_unstarted_holds_labelled_with_no_unblocker_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            path = self.v12_ledger(project)
            self.hold_v12(path, "work-c", "blocked")
            self.hold_v12(path, "zz-proposal-a", "deferred")
            self.hold_v12(path, "work-a", "blocked")
            with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE attempts SET state = 'blocked' WHERE attempt_id = 'work-a-1'")
            source = hashlib.sha256(path.read_bytes()).hexdigest()
            condition: JsonObject = {"kind": "outside-condition", "condition": "The vendor publishes its v2 API."}
            released = [
                {"item_id": "work-c", "attempt_id": None, "unblocker": None},
                {"item_id": "zz-proposal-a", "attempt_id": None, "unblocker": None},
            ]
            started = self.write_labels(
                path, source, [*released, {"item_id": "work-a", "attempt_id": "work-a-1", "unblocker": None}]
            )
            code, rejected = self.run_cli(project, "migrate-schema", "--labels", str(started))
            self.assertNotEqual(0, code)
            self.assertIn("only unstarted work can be released", str(rejected["message"]))
            label_file = self.write_labels(
                path, source, [*released, {"item_id": "work-a", "attempt_id": "work-a-1", "unblocker": condition}]
            )
            code, planned = self.run_cli(project, "migrate-schema", "--labels", str(label_file))
            self.assertEqual(0, code, planned)
            code, migrated = self.run_cli(
                project, "migrate-schema", "--apply", str(planned["plan_id"]), "--labels", str(label_file)
            )
            self.assertEqual(0, code, migrated)
            reloaded = SQLiteWorkStore(advanced_v13_copy(path)).validated_snapshot()
            states = {value.item_id: value.state for value in reloaded.lifecycle.work_items}
            self.assertEqual(stored_state.StoredWorkItemState.READY, states[WorkItemId("work-c")])
            self.assertEqual(stored_state.StoredWorkItemState.READY, states[WorkItemId("zz-proposal-a")])
            self.assertEqual(stored_state.StoredWorkItemState.BLOCKED, states[WorkItemId("work-a")])
            self.assertEqual(
                {WorkItemId("work-a"): work_models.WaitingForOutsideCondition("The vendor publishes its v2 API.")},
                {value.item_id: value.unblocker for value in reloaded.lifecycle.unblockers},
            )

    def test_v12_paused_attempts_are_labelled_from_their_receipts_or_listed_for_a_label(self) -> None:
        pending = {
            "schema": "pinboard-pause-decisions/v1",
            "reason": "Two choices remain.",
            "decisions": [
                {
                    "decision_id": "audience",
                    "question": "Who reads it?",
                    "options": ["Users", "Admins"],
                    "recommendation": "Users",
                    "unblocks": "Scope",
                    "deferral_cost": "Delay",
                }
            ],
        }
        plain: dict[str, object] = {"evidence": "Wait for the legal review.", "outcome": "pause"}
        empty: dict[str, object] = {}
        checkpoint = {
            "candidate": "candidate-a",
            "checkpoint": "routes",
            "evidence": "Verified.",
            "outcome": "accept-checkpoint",
        }
        for case, action, input_schema, input_json, outcome_schema, outcome_json, batch in (
            ("plain", "pause", "decision/v1", empty, "transition-receipt/v1", plain, None),
            ("pending", "pause", "pinboard-pause-decisions/v1", pending, "transition-receipt/v1", plain, "pending"),
            ("resolved", "pause", "decision/v1", empty, "transition-receipt/v1", plain, "resolved"),
            ("checkpoint", "accept-checkpoint", "decision/v1", empty, "checkpoint-acceptance/v2", checkpoint, None),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = self.v12_ledger(Path(directory).resolve())
                self.hold_v12(path, "work-a", "paused")
                with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                    revision = connection.execute("SELECT revision + 1 FROM project_meta").fetchone()[0]
                    history = connection.execute(
                        "SELECT coalesce(max(history_id), 0) + 1 FROM transition_history"
                    ).fetchone()[0]
                    if batch is not None:
                        connection.execute(
                            """INSERT INTO transition_history (history_id, project_revision, action_id, action_kind,
                               subject_id, authorization_kind, input_schema, input_json, outcome_schema, outcome_json,
                               committed_at) VALUES (?, ?, 'pause:work-a-1', 'pause', 'work-a-1', 'project',
                               'pinboard-pause-decisions/v1', ?, 'transition-receipt/v1', ?, ?)""",
                            (history, revision, json.dumps(pending), json.dumps(plain), SQLITE_NOW.isoformat()),
                        )
                        connection.execute(
                            "INSERT INTO attempt_decision_batches VALUES ('work-a-1', ?, ?)",
                            (history, None if batch == "pending" else history + 1),
                        )
                        history, revision = history + 1, revision + 1
                    if batch != "pending":
                        connection.execute(
                            """INSERT INTO transition_history (history_id, project_revision, action_id, action_kind,
                               subject_id, authorization_kind, input_schema, input_json, outcome_schema, outcome_json,
                               committed_at) VALUES (?, ?, ?, ?, 'work-a-1', 'project', ?, ?, ?, ?, ?)""",
                            (
                                history,
                                revision,
                                f"{action}:work-a-1",
                                "resume" if batch == "resolved" else action,
                                input_schema,
                                json.dumps(input_json),
                                outcome_schema,
                                json.dumps(outcome_json),
                                SQLITE_NOW.isoformat(),
                            ),
                        )
                        if batch == "resolved":
                            connection.execute(
                                """INSERT INTO transition_history (history_id, project_revision, action_id, action_kind,
                                   subject_id, authorization_kind, input_schema, input_json, outcome_schema,
                                   outcome_json, committed_at) VALUES (?, ?, 'pause:work-a-1', 'pause', 'work-a-1',
                                   'project', 'decision/v1', '{}', 'transition-receipt/v1', ?, ?)""",
                                (history + 1, revision + 1, json.dumps(plain), SQLITE_NOW.isoformat()),
                            )
                            revision += 1
                    connection.execute("UPDATE project_meta SET revision = ?", (revision,))
                    connection.execute(
                        "UPDATE attempts SET state = 'paused', subject_revision = ? WHERE attempt_id = 'work-a-1'",
                        (revision,),
                    )
                with contextlib.closing(sqlite3.connect(path)) as connection:
                    connection.row_factory = sqlite3.Row
                    automatic, needed = schema_unblockers_v12.classify_holds(connection)
                match case:
                    case "pending":
                        self.assertEqual((), needed)
                        derived = automatic[WorkItemId("work-a")]
                        assert isinstance(derived, work_models.WaitingForHumanDecision)
                        self.assertIn("audience", derived.question)
                    case "checkpoint":
                        self.assertEqual((), needed)
                        derived = automatic[WorkItemId("work-a")]
                        assert isinstance(derived, work_models.WaitingForHumanDecision)
                        self.assertIn("routes", derived.question)
                    case _:
                        self.assertEqual({}, automatic)
                        self.assertEqual(("work-a",), tuple(str(value.item_id) for value in needed))
                        self.assertIn("Wait for the legal review.", needed[0].proposal)

    def test_populated_v6_ledger_migrates_explicitly_and_failed_upgrade_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            path = resolve_durable_roots(project).database_path
            store = SQLiteWorkStore(path)
            initialize_store(store, complete_sqlite_state())
            expected = self.predecessor_rows(path)
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
                    "pinboard.adapters.sqlite.migration.schema_procedure.migrate_v6_database",
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
            repeated_code, repeated = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(0, repeated_code, repeated)
            self.assertEqual("unchanged", repeated["status"])
            current_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", current_id)[0])
            self.assertEqual(expected, self.predecessor_rows(path))
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertEqual(8, connection.execute("SELECT schema_version FROM project_meta").fetchone()[0])

    def test_schema_apply_reports_committed_effect_without_a_second_database_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            self.assertEqual(0, self.run_cli(project, "init")[0])
            path = resolve_durable_roots(project).database_path
            self.downgrade_schema(path)
            plan_id = str(self.run_cli(project, "migrate-schema")[1]["plan_id"])
            with patch(
                "pinboard.cli.migration.schema_migration.inspect_current_schema_migration",
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
            self.assertEqual(12, self.run_cli(project, "status")[0])
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE project_meta SET revision = revision + 1")
            code, refused = self.run_cli(project, "migrate-schema", "--apply", str(reverse["plan_id"]))
            self.assertEqual(11, code, refused)
            self.assertEqual([], refused["changed_surfaces"])
            self.assertEqual(before, backup.read_bytes())
            self.assertEqual(12, self.run_cli(project, "status")[0])

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

            with patch(
                "pinboard.adapters.sqlite.migration.schema_procedure.create_immutable", side_effect=publish_then_fail
            ):
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
            expected = self.predecessor_rows(roots.database_path)
            self.downgrade_schema(roots.database_path)
            original = roots.database_path.read_bytes()
            forward_id = str(self.run_cli(project, *args, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", forward_id)[0])
            reverse_id = str(self.run_cli(project, *args, "migrate-schema", "--reverse", forward_id)[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", reverse_id)[0])
            self.assertEqual(original, roots.database_path.read_bytes())
            self.assertEqual(original, (selected / "migration" / f"{forward_id}.v6").read_bytes())
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", forward_id)[0])
            current_id = str(self.run_cli(project, *args, "migrate-schema")[1]["plan_id"])
            self.assertEqual(0, self.run_cli(project, *args, "migrate-schema", "--apply", current_id)[0])
            self.assertEqual(expected, self.predecessor_rows(roots.database_path))
            with contextlib.closing(sqlite3.connect(roots.database_path)) as connection:
                self.assertEqual(8, connection.execute("SELECT schema_version FROM project_meta").fetchone()[0])
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
                            selected, publication.reference, SQLITE_NOW, ownership=None
                        )
                    )
                    fresh = SQLiteWorkStore(roots.database_path).validated_snapshot()
                    self.assertEqual(b"immutable", ArtifactRepository(roots).read(fresh.artifact_references[0]))
                    self.assertEqual(0, self.run_cli(project, *args, "status")[0])
            self.assertFalse((project / ".pinboard").exists())

    def test_symlinked_work_roots_and_parents_resolve_canonically_and_publish_and_report_broken_links(self) -> None:
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
                    current.work_root, publication.reference, SQLITE_NOW, ownership=None
                )
            )
            reference = SQLiteWorkStore(current.database_path).validated_snapshot().artifact_references[0]
            alias = project / ".codex" / "pinboard"
            alias.parent.mkdir()
            alias.symlink_to("../.pinboard", target_is_directory=True)
            selected = resolve_durable_roots(project, alias)
            self.assertEqual(current.work_root, selected.work_root)
            self.assertEqual(0, self.run_cli(project, "--work-root", str(alias), "status")[0])
            self.assertEqual(0, self.run_cli(project, "--work-root", str(alias), "validate")[0])
            self.assertEqual(b"immutable", ArtifactRepository(selected).read(reference))
            second = ArtifactRepository(selected).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "new", 1, ".txt", b"new")
            )
            assert isinstance(second, ArtifactPublication)
            self.assertTrue((current.work_root / second.reference.selector).is_file())

            linked_parent = project / "linked-parent"
            linked_parent.symlink_to(alias.parent, target_is_directory=True)
            linked_roots = resolve_durable_roots(project, linked_parent / "work")
            self.assertEqual(alias.parent / "work", linked_roots.work_root)
            self.assertEqual(0, self.run_cli(project, "--work-root", str(linked_parent / "work"), "init")[0])

            missing_target = project / "missing-board"
            dangling_alias = project / "dangling-board"
            dangling_alias.symlink_to(missing_target, target_is_directory=True)
            with self.assertRaises(FileIOError) as broken:
                resolve_durable_roots(project, dangling_alias)
            self.assertEqual(FileIOErrorCode.DIRECTORY_VERIFY_FAILED, broken.exception.code)
            self.assertIn(str(dangling_alias), str(broken.exception))
            self.assertIn(str(missing_target), str(broken.exception))
            self.assertIn(
                f"Create {missing_target}, or fix or remove the link {dangling_alias}, then retry.",
                str(broken.exception),
            )

    def test_v14_step_releases_only_releasable_unstarted_holds_rejects_labels_and_reverses_until_a_new_write(  # noqa: PLR0915 - one v14 hold-release migration, refusal and reversal journey
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            brief_model = work_a_brief(project)
            subprocess.run(["git", "add", "architecture.md"], cwd=project, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
                cwd=project,
                check=True,
                capture_output=True,
            )
            self.assertEqual(0, self.run_cli(project, "init")[0])
            roots = resolve_durable_roots(project)
            path = roots.database_path
            published_brief = write_revision(
                roots,
                NewArtifact(
                    work_models.ArtifactKind.BRIEF,
                    brief_model.attempt_id,
                    brief_model.artifact_revision,
                    ".json",
                    canonical_work_brief_bytes(brief_model),
                ),
            )
            base = complete_sqlite_state()
            extra_defs = []

            def _def_digest(defn: stored_state.StoredDefinition) -> str:
                encoded = stored_state.stored_definition_bytes(defn)
                assert isinstance(encoded, bytes)
                return hashlib.sha256(encoded).hexdigest()

            for item_id, deps in (
                (WorkItemId("dropped-item"), ()),
                (WorkItemId("replacer"), ()),
                (WorkItemId("outside-held"), (WorkItemId("work-b"),)),
                (WorkItemId("cancelled-held"), (WorkItemId("dropped-item"),)),
                (WorkItemId("replacement-held"), (WorkItemId("work-b"),)),
                (WorkItemId("releasable-unstarted"), (WorkItemId("work-b"),)),
            ):
                base_def, _ = test_definition(item_id)
                updated_def = dataclass_replace(base_def, dependencies=deps)
                digest = _def_digest(updated_def)
                extra_defs.append(
                    stored_state.ItemDefinitionRevision(
                        item_id,
                        1,
                        digest,
                        updated_def,
                        "Accepted test definition.",
                        TaskId("test-source"),
                        None,
                        digest,
                        3,
                        SQLITE_NOW,
                    )
                )
            work_a_def, _ = test_definition(WorkItemId("work-a"))
            work_a_updated = dataclass_replace(work_a_def, dependencies=(WorkItemId("work-b"),))
            work_a_digest = _def_digest(work_a_updated)
            intake_def, _ = test_definition(WorkItemId("intake-work"))
            intake_updated = dataclass_replace(intake_def, dependencies=(WorkItemId("work-b"), WorkItemId("work-c")))
            intake_digest = _def_digest(intake_updated)
            proposal_current = next(
                v.definition for v in base.lifecycle.definition_revisions if v.item_id == WorkItemId("zz-proposal-a")
            )
            proposal_updated = dataclass_replace(proposal_current, dependencies=(WorkItemId("work-b"),))
            proposal_digest = _def_digest(proposal_updated)

            updated_brief = canonical_work_brief_bytes(
                msgspec.structs.replace(
                    brief_model,
                    accepted_scope=msgspec.structs.replace(
                        brief_model.accepted_scope, revision=1, digest=work_a_digest
                    ),
                )
            )
            published_brief = write_revision(
                roots,
                NewArtifact(work_models.ArtifactKind.BRIEF, brief_model.attempt_id, 2, ".json", updated_brief),
            )
            write_revision(
                roots, NewArtifact(work_models.ArtifactKind.REQUIREMENTS, "work-a-requirements", 1, ".md", b"# Req\n")
            )
            write_revision(
                roots, NewArtifact(work_models.ArtifactKind.EVIDENCE, "work-a-evidence", 1, ".md", b"# Ev\n")
            )

            definitions = tuple(
                dataclass_replace(value, definition=work_a_updated, digest=work_a_digest, after_digest=work_a_digest)
                if value.item_id == WorkItemId("work-a")
                else dataclass_replace(
                    value, definition=intake_updated, digest=intake_digest, after_digest=intake_digest
                )
                if value.item_id == WorkItemId("intake-work")
                else dataclass_replace(
                    value, definition=proposal_updated, digest=proposal_digest, after_digest=proposal_digest
                )
                if value.item_id == WorkItemId("zz-proposal-a")
                else value
                for value in base.lifecycle.definition_revisions
            ) + tuple(extra_defs)
            work_items = (
                stored_state.StoredWorkItem(
                    WorkItemId("intake-work"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    1,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("work-a"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    work_models.Timing.MUST_NOW,
                    "accepted requirement",
                    None,
                    "continue",
                    "Current work remains bounded.",
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    2,
                ),
                base.lifecycle.work_items[2],
                base.lifecycle.work_items[3],
                stored_state.StoredWorkItem(
                    WorkItemId("zz-proposal-a"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    None,
                    "proposal:zz-proposal-a",
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    4,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("dropped-item"),
                    stored_state.StoredWorkItemState.DROPPED,
                    None,
                    None,
                    "Cancelled.",
                    None,
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    None,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("replacer"),
                    stored_state.StoredWorkItemState.READY,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    5,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("outside-held"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    6,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("cancelled-held"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    7,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("replacement-held"),
                    stored_state.StoredWorkItemState.BLOCKED,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    8,
                ),
                stored_state.StoredWorkItem(
                    WorkItemId("releasable-unstarted"),
                    stored_state.StoredWorkItemState.READY,
                    None,
                    None,
                    None,
                    "activate",
                    None,
                    7,
                    SQLITE_NOW,
                    SQLITE_NOW,
                    9,
                ),
            )
            dependencies = (
                stored_state.ItemDependency(WorkItemId("work-a"), WorkItemId("work-b"), 0, checkpoint_id=None),
                stored_state.ItemDependency(WorkItemId("intake-work"), WorkItemId("work-b"), 0, checkpoint_id=None),
                stored_state.ItemDependency(WorkItemId("intake-work"), WorkItemId("work-c"), 1, checkpoint_id=None),
                stored_state.ItemDependency(WorkItemId("zz-proposal-a"), WorkItemId("work-b"), 0, checkpoint_id=None),
                stored_state.ItemDependency(WorkItemId("outside-held"), WorkItemId("work-b"), 0, checkpoint_id=None),
                stored_state.ItemDependency(
                    WorkItemId("cancelled-held"), WorkItemId("dropped-item"), 0, checkpoint_id=None
                ),
                stored_state.ItemDependency(
                    WorkItemId("replacement-held"), WorkItemId("work-b"), 0, checkpoint_id=None
                ),
                stored_state.ItemDependency(
                    WorkItemId("releasable-unstarted"), WorkItemId("work-b"), 0, checkpoint_id=None
                ),
            )
            attempts = (
                dataclass_replace(
                    base.lifecycle.attempts[0],
                    state=work_models.AttemptState.BLOCKED,
                    accepted_scope_digest=work_a_digest,
                    brief_artifact_ref_id=base.artifact_references[0].artifact_ref_id,
                ),
            )
            unblockers = (
                stored_state.StoredUnblocker(
                    WorkItemId("work-a"), work_models.WaitingForItemsToFinish((WorkItemId("work-b"),))
                ),
                stored_state.StoredUnblocker(
                    WorkItemId("intake-work"),
                    work_models.WaitingForItemsToFinish((WorkItemId("work-b"), WorkItemId("work-c"))),
                ),
                stored_state.StoredUnblocker(
                    WorkItemId("zz-proposal-a"), work_models.WaitingForHumanDecision("Approve legacy?")
                ),
                stored_state.StoredUnblocker(
                    WorkItemId("outside-held"), work_models.WaitingForOutsideCondition("DNS propagated.")
                ),
                stored_state.StoredUnblocker(
                    WorkItemId("cancelled-held"), work_models.WaitingForItemsToFinish((WorkItemId("dropped-item"),))
                ),
                stored_state.StoredUnblocker(
                    WorkItemId("replacement-held"), work_models.WaitingForItemsToFinish((WorkItemId("work-b"),))
                ),
            )
            references = (
                dataclass_replace(
                    base.artifact_references[0],
                    key=published_brief.key,
                    revision=published_brief.revision,
                    selector=published_brief.selector,
                    content_sha256=published_brief.content_sha256,
                    size_bytes=published_brief.size_bytes,
                ),
                dataclass_replace(
                    base.artifact_references[1],
                    selector="artifacts/requirements/work-a-requirements/1.md",
                    content_sha256=hashlib.sha256(b"# Req\n").hexdigest(),
                    size_bytes=len(b"# Req\n"),
                ),
                dataclass_replace(
                    base.artifact_references[2],
                    selector="artifacts/evidence/work-a-evidence/1.md",
                    content_sha256=hashlib.sha256(b"# Ev\n").hexdigest(),
                    size_bytes=len(b"# Ev\n"),
                ),
            )
            replacements = stored_state.ReplacementRecords(
                (
                    stored_state.StoredPlannedReplacement(
                        WorkItemId("replacement-held"),
                        1,
                        WorkItemId("replacer"),
                        "Replaced by replacer.",
                        work_models.PlannedReplacementStatus.CURRENT,
                        TaskId("coordinator"),
                        SQLITE_NOW,
                        3,
                    ),
                ),
                (),
            )
            proposals = stored_state.ProposalRecords(
                tuple(
                    dataclass_replace(
                        value,
                        relation=work_models.FollowUpProposalRelation(WorkItemId("work-b")),
                    )
                    for value in base.proposals.proposals
                )
            )
            state = dataclass_replace(
                base,
                lifecycle=dataclass_replace(
                    base.lifecycle,
                    work_items=work_items,
                    dependencies=dependencies,
                    attempts=attempts,
                    definition_revisions=definitions,
                    unblockers=unblockers,
                ),
                artifact_references=references,
                replacements=replacements,
                proposals=proposals,
            )
            initialize_store(SQLiteWorkStore(path), state)
            downgrade_to_v14(path)
            with contextlib.closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE work_items SET state = 'blocked' WHERE item_id = 'releasable-unstarted'")
                connection.execute(
                    "UPDATE work_item_state_counts SET item_count = item_count - 1 WHERE state = 'ready'"
                )
                connection.execute(
                    "UPDATE work_item_state_counts SET item_count = item_count + 1 WHERE state = 'blocked'"
                )
                connection.execute(
                    "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
                    ("releasable-unstarted", '{"items":["work-b"],"kind":"items"}'),
                )
                sqlite_database._verify_v14_schema(connection)
            original = path.read_bytes()
            revision_before = state.lifecycle.project.revision
            history_count_before = len(state.transition_receipts)

            for subcommand in (("status",), ("export",)):
                code, refused_cli = self.run_cli(project, *subcommand)
                self.assertEqual(12, code, subcommand)
                self.assertEqual("SCHEMA_UNSUPPORTED", refused_cli["code"])
                self.assertIn("sqlite-v14", str(refused_cli["message"]))
                self.assertIn("sqlite-v15", str(refused_cli["message"]))
                self.assertIn("migrate-schema", str(refused_cli["message"]))
            validate_code, refused_validate = self.run_cli(project, "validate")
            self.assertNotEqual(0, validate_code, refused_validate)
            self.assertFalse(refused_validate["valid"], refused_validate)
            self.assertIn("sqlite-v14", str(refused_validate["diagnostics"]))
            self.assertIn("sqlite-v15", str(refused_validate["diagnostics"]))
            refused_mcp = call_advertised_tool(
                mcp_server.OVERVIEW_TOOL,
                {
                    "project_root": str(project),
                    "work_root": str(roots.work_root),
                    "max_items": 25,
                    "continuation": None,
                },
            )
            self.assertEqual(("rejected", "SCHEMA_UNSUPPORTED"), (refused_mcp["status"], refused_mcp["code"]))
            self.assertEqual(original, path.read_bytes())

            label_file = self.write_labels(path, hashlib.sha256(original).hexdigest(), [])
            code, rejected_labels = self.run_cli(project, "migrate-schema", "--labels", str(label_file))
            self.assertNotEqual(0, code)
            self.assertIn("only to the v12 step", str(rejected_labels["message"]))
            self.assertIn("sqlite-v14", str(rejected_labels["message"]))
            self.assertEqual(original, path.read_bytes())

            code, planned = self.run_cli(project, "migrate-schema")
            self.assertEqual(0, code, planned)
            plan = planned["plan"]
            assert isinstance(plan, dict)
            self.assertEqual("pinboard-schema-plan/v9", plan["schema"])
            self.assertEqual("hold-releases-advance", plan["kind"])
            self.assertEqual(
                [
                    {
                        "item_id": "releasable-unstarted",
                        "previous_state": "blocked",
                        "previous_subject_revision": 7,
                        "unblocker": {"kind": "items", "items": ["work-b"]},
                    }
                ],
                plan["released_holds"],
            )
            self.assertEqual(original, path.read_bytes())

            plan_id = str(planned["plan_id"])
            code, migrated = self.run_cli(project, "migrate-schema", "--apply", plan_id)
            self.assertEqual(0, code, migrated)
            self.assertEqual(
                ("migrated", "sqlite-v15", None), (migrated["status"], migrated["authority"], migrated["warning"])
            )
            evidence = path.parent / "migration"
            self.assertTrue((evidence / f"{plan_id}.json").is_file())
            self.assertEqual(original, (evidence / f"{plan_id}.v14").read_bytes())

            reloaded = SQLiteWorkStore(path).validated_snapshot()
            self.assertEqual(15, reloaded.lifecycle.project.schema_version)
            self.assertEqual(revision_before, reloaded.lifecycle.project.revision)
            self.assertEqual(history_count_before, len(reloaded.transition_receipts))
            self.assertEqual((), reloaded.lifecycle.released_holds)
            states = {value.item_id: value.state for value in reloaded.lifecycle.work_items}
            self.assertEqual(stored_state.StoredWorkItemState.READY, states[WorkItemId("releasable-unstarted")])
            for kept_held in (
                "work-a",
                "intake-work",
                "zz-proposal-a",
                "outside-held",
                "cancelled-held",
                "replacement-held",
            ):
                self.assertEqual(stored_state.StoredWorkItemState.BLOCKED, states[WorkItemId(kept_held)], kept_held)
            self.assertNotIn(
                WorkItemId("releasable-unstarted"),
                {value.item_id for value in reloaded.lifecycle.unblockers},
            )
            self.assertEqual(0, self.run_cli(project, "validate")[0])
            code, exported = self.run_cli(project, "export")
            self.assertEqual(0, code, exported)
            self.assertEqual(
                ("pinboard-project-export/v7", "sqlite-v15", [], []),
                (exported["schema"], exported["authority"], exported["amendments"], exported["released_holds"]),
            )

            unchanged = self.run_cli(project, "migrate-schema", "--apply", plan_id)[1]
            self.assertEqual("unchanged", unchanged["status"])
            reverse = self.run_cli(project, "migrate-schema", "--reverse", plan_id)[1]
            reversed_result = self.run_cli(project, "migrate-schema", "--apply", str(reverse["plan_id"]))[1]
            self.assertEqual(("reversed", "sqlite-v14"), (reversed_result["status"], reversed_result["authority"]))
            self.assertEqual(original, path.read_bytes())

            self.assertEqual(0, self.run_cli(project, "migrate-schema", "--apply", plan_id)[0])
            publication = ArtifactRepository(roots).publish(
                NewArtifact(work_models.ArtifactKind.EVIDENCE, "after-v15", 1, ".txt", b"after")
            )
            assert isinstance(publication, ArtifactPublication)
            expect_success(
                SQLiteWorkStore(path).accept_artifact_reference(
                    roots.work_root, publication.reference, SQLITE_NOW, ownership=None
                )
            )
            later = path.read_bytes()
            code, refused_reverse = self.run_cli(project, "migrate-schema", "--reverse", plan_id)
            self.assertNotEqual(0, code)
            self.assertIn("changed after schema migration", str(refused_reverse["message"]))
            self.assertEqual(later, path.read_bytes())

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
