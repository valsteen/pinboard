"""Exact v9 predecessor facts, receipt backfill, and bound v10 advancement."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import database, schema_procedure
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.domain.history import encode_checkpoint_acceptance_outcome, work_item_definition_bytes
from pinboard.domain.identifiers import WorkItemId
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, test_definition


class DependencyMigrationTest(unittest.TestCase):
    def test_retained_v4_current_plan_still_means_unchanged_v10(self) -> None:
        path = self.predecessor()
        advancement = schema_procedure.preview_schema(path)
        assert isinstance(advancement, schema_procedure.PlannedSchema), advancement
        self.assertIsInstance(schema_procedure.apply_schema(path, advancement.plan_id), schema_procedure.AppliedSchema)
        original = path.read_bytes()
        retained = schema_procedure.DependencyCurrentPlan(
            "pinboard-schema-plan/v4", str(path), sha256(original).hexdigest()
        )
        content = msgspec.json.encode(retained, order="sorted")
        plan_id = sha256(content).hexdigest()
        (path.parent / "migration" / f"{plan_id}.json").write_bytes(content)
        result = schema_procedure.apply_schema(path, plan_id)
        assert isinstance(result, schema_procedure.AppliedSchema), result
        self.assertEqual((10, "unchanged", ()), (result.resulting_version, result.direction, result.changed_surfaces))
        self.assertEqual(original, path.read_bytes())
        with database.migration_connection(path) as connection:
            database._verify_v10_schema(connection)
        next_step = schema_procedure.preview_schema(path)
        assert isinstance(next_step, schema_procedure.PlannedSchema), next_step
        self.assertIsInstance(next_step.plan, schema_procedure.DecisionBatchAdvancePlan)

    def predecessor(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name).resolve() / "state.sqlite3"
        schema = Path(database.__file__).with_name("schema_compatibility_v9.sql").read_text()
        timestamp = SQLITE_NOW.isoformat()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(schema)
            connection.execute("INSERT INTO project_meta VALUES (1, 'pinboard', 9, 2, 1, ?, ?)", (timestamp, timestamp))
            connection.executemany(
                """INSERT INTO work_items
                   (item_id, state, subject_revision, recorded_at, updated_at, queue_position)
                   VALUES (?, 'ready', 0, ?, ?, ?)""",
                (("producer", timestamp, timestamp, 1), ("consumer", timestamp, timestamp, 2)),
            )
            connection.execute("INSERT INTO item_dependencies VALUES ('consumer', 'producer', 0)")
            for item_id in (WorkItemId("producer"), WorkItemId("consumer")):
                definition, digest = test_definition(item_id)
                connection.execute(
                    "INSERT INTO work_item_definition_revisions VALUES (?, 1, ?, ?, 'Accepted.', 'human', NULL, ?, 0, ?)",
                    (item_id, digest, expect_success(work_item_definition_bytes(definition)), digest, timestamp),
                )
            connection.execute(
                """INSERT INTO proposals
                   (proposal_id, created_at, recorded_at, source_task_id, user_label, trigger, why_it_matters,
                    relation_kind, relation_item_id, effect, unlock, urgency_evidence, subject_revision)
                   VALUES ('consumer', ?, ?, 'human', 'Consumer', 'Need API', 'Use accepted API',
                           'follow-up', 'producer', 'Consumer works', 'Integration', 'None', 0)""",
                (timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO artifact_refs VALUES
                   (1, 'producer-brief', 1, 'brief', 'artifacts/brief/producer.json', ?, 0, 0, ?)""",
                ("a" * 64, timestamp),
            )
            for number in (1, 2):
                connection.execute(
                    """INSERT INTO attempts
                       (attempt_id, item_id, state, branch, base_revision, provenance, brief_artifact_ref_id,
                        brief_artifact_kind, accepted_scope_revision, accepted_scope_digest, subject_revision,
                        recorded_at, updated_at)
                       VALUES (?, 'producer', 'done', 'main', 'base', 'Accepted fixture', 1, 'brief', 1, ?, 0, ?, ?)""",
                    (f"producer-{number}", "b" * 64, timestamp, timestamp),
                )
                outcome = encode_checkpoint_acceptance_outcome(
                    candidate=f"candidate-{number}",
                    checkpoint="usable-api",
                    evidence="Accepted.",
                    outcome="accept-checkpoint",
                ).decode()
                connection.execute(
                    """INSERT INTO transition_history
                       (history_id, project_revision, action_id, action_kind, subject_id, authorization_kind,
                        input_schema, input_json, outcome_schema, outcome_json, committed_at)
                       VALUES (?, ?, ?, 'accept-checkpoint', ?, 'project', 'decision/v1', '{}',
                               'checkpoint-acceptance/v2', ?, ?)""",
                    (number, number, f"accept-{number}", f"producer-{number}", outcome, timestamp),
                )
            database._verify_v9_schema(connection)
        return path

    def test_explicit_advancement_preserves_facts_backfills_every_receipt_and_reverses_exactly(self) -> None:
        path = self.predecessor()
        original = path.read_bytes()
        with closing(sqlite3.connect(path)) as connection:
            receipts = connection.execute("SELECT * FROM transition_history ORDER BY history_id").fetchall()
            edges = connection.execute("SELECT item_id, dependency_id, position FROM item_dependencies").fetchall()
            definitions = connection.execute("SELECT * FROM work_item_definition_revisions ORDER BY item_id").fetchall()
            proposals = connection.execute("SELECT * FROM proposals").fetchall()
        with self.assertRaises(StorageError) as refused:
            database.open_database(path, OpenMode.READ_ONLY)
        self.assertEqual(StorageErrorCode.SCHEMA_UNSUPPORTED, refused.exception.code)
        plan = schema_procedure.preview_schema(path)
        assert isinstance(plan, schema_procedure.PlannedSchema), plan
        self.assertIsInstance(plan.plan, schema_procedure.DependencyAdvancePlan)
        self.assertEqual(original, path.read_bytes())
        self.assertFalse((path.parent / "migration").exists())
        result = schema_procedure.apply_schema(path, plan.plan_id)
        assert isinstance(result, schema_procedure.AppliedSchema), result
        self.assertEqual(10, result.resulting_version)
        self.assertEqual(original, (path.parent / "migration" / f"{plan.plan_id}.v9").read_bytes())
        with database.migration_connection(path) as connection:
            database._verify_v10_schema(connection)
            self.assertEqual(
                receipts,
                [tuple(row) for row in connection.execute("SELECT * FROM transition_history ORDER BY history_id")],
            )
            self.assertEqual(
                edges,
                [
                    tuple(row)
                    for row in connection.execute("SELECT item_id, dependency_id, position FROM item_dependencies")
                ],
            )
            self.assertEqual(
                definitions,
                [
                    tuple(row)
                    for row in connection.execute("SELECT * FROM work_item_definition_revisions ORDER BY item_id")
                ],
            )
            self.assertEqual(proposals, [tuple(row)[:-1] for row in connection.execute("SELECT * FROM proposals")])
            self.assertEqual(
                [None], [row[0] for row in connection.execute("SELECT relation_checkpoint_id FROM proposals")]
            )
            self.assertEqual(
                [(1, "producer", "usable-api"), (2, "producer", "usable-api")],
                [tuple(row) for row in connection.execute("SELECT * FROM checkpoint_acceptances ORDER BY history_id")],
            )
            self.assertEqual(
                [None], [row[0] for row in connection.execute("SELECT checkpoint_id FROM item_dependencies")]
            )
        repeated = schema_procedure.apply_schema(path, plan.plan_id)
        assert isinstance(repeated, schema_procedure.AppliedSchema), repeated
        self.assertEqual((), repeated.changed_surfaces)
        reverse = schema_procedure.preview_schema(path, plan.plan_id)
        assert isinstance(reverse, schema_procedure.PlannedSchema), reverse
        self.assertIsInstance(reverse.plan, schema_procedure.DependencyRestorePlan)
        self.assertIsInstance(schema_procedure.apply_schema(path, reverse.plan_id), schema_procedure.AppliedSchema)
        self.assertEqual(original, path.read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, reverse.plan_id), schema_procedure.AppliedSchema)
        self.assertIsInstance(schema_procedure.apply_schema(path, plan.plan_id), schema_procedure.AppliedSchema)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE project_meta SET revision = revision + 1")
        changed = path.read_bytes()
        self.assertIsInstance(
            schema_procedure.preview_schema(path, plan.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertEqual(changed, path.read_bytes())

    def test_transaction_failure_retains_predecessor_and_exact_backup_then_resumes(self) -> None:
        path = self.predecessor()
        original = path.read_bytes()
        plan = schema_procedure.preview_schema(path)
        assert isinstance(plan, schema_procedure.PlannedSchema), plan
        verify = database._verify_v10_schema

        def reject_live_postimage(connection: sqlite3.Connection) -> None:
            verify(connection)
            if connection.execute("PRAGMA database_list").fetchone()[2] == str(path):
                raise StorageError(StorageErrorCode.INVALID_STATE, "controlled failure before commit")

        with patch.object(database, "_verify_v10_schema", side_effect=reject_live_postimage):
            failed = schema_procedure.apply_schema(path, plan.plan_id)
        assert isinstance(failed, schema_procedure.SchemaProcedureFailure), failed
        self.assertEqual(("migration-evidence",), failed.changed_surfaces)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(original, (path.parent / "migration" / f"{plan.plan_id}.v9").read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, plan.plan_id), schema_procedure.AppliedSchema)

    def test_invalid_receipt_and_missing_attempt_reject_without_changing_predecessor(self) -> None:
        for condition in ("malformed", "unknown-field", "noncanonical", "foreign-subject", "wrong-action"):
            with self.subTest(condition=condition):
                path = self.predecessor()
                with closing(sqlite3.connect(path)) as connection, connection:
                    if condition == "foreign-subject":
                        connection.execute("UPDATE transition_history SET subject_id = 'missing' WHERE history_id = 1")
                    elif condition == "wrong-action":
                        connection.execute("UPDATE transition_history SET action_kind = 'pause' WHERE history_id = 1")
                    elif condition == "malformed":
                        connection.execute("UPDATE transition_history SET outcome_json = '{}' WHERE history_id = 1")
                    elif condition == "unknown-field":
                        connection.execute("""UPDATE transition_history
                            SET outcome_json = substr(outcome_json, 1, length(outcome_json) - 1) || ',"extra":"unowned"}'
                            WHERE history_id = 1""")
                    else:
                        connection.execute(
                            "UPDATE transition_history SET outcome_json = outcome_json || ' ' WHERE history_id = 1"
                        )
                original = path.read_bytes()
                refused = schema_procedure.preview_schema(path)
                assert isinstance(refused, schema_procedure.SchemaProcedureFailure), refused
                self.assertIn("Checkpoint receipt '1'", refused.message)
                self.assertEqual(original, path.read_bytes())
                self.assertFalse((path.parent / "migration").exists())

    def test_fresh_current_initialization_has_an_empty_checkpoint_relation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            roots = resolve_durable_roots(Path(directory).resolve())
            database.initialize_database(roots, SQLITE_NOW)
            with closing(database.open_database(roots.database_path, OpenMode.READ_ONLY)) as connection:
                self.assertEqual(0, connection.execute("SELECT count(*) FROM checkpoint_acceptances").fetchone()[0])
            plan = schema_procedure.preview_schema(roots.database_path)
            assert isinstance(plan, schema_procedure.PlannedSchema), plan
            self.assertIsInstance(plan.plan, schema_procedure.DecisionBatchCurrentPlan)
            unchanged = schema_procedure.apply_schema(roots.database_path, plan.plan_id)
            assert isinstance(unchanged, schema_procedure.AppliedSchema), unchanged
            self.assertEqual((11, ()), (unchanged.resulting_version, unchanged.changed_surfaces))
