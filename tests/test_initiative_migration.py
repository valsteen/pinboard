"""Exact populated predecessor migration and retained-plan meaning on disposable ledgers."""

import contextlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.sqlite import database, schema_procedure
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from tests import test_initiatives, test_storage_migration
from tests.checkpoint_support import CheckpointPackageSupport


class InitiativeMigrationTest(CheckpointPackageSupport):
    def v7(self, path: Path) -> None:
        # The released v7 shape differs only by the new initiative relations and metadata identity.
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            for table in (
                "initiative_assessments",
                "initiative_decisions",
                "initiative_memberships",
                "initiative_revisions",
                "initiative_contexts",
                "initiatives",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v8")
            connection.execute("""CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 7),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""")
            connection.execute(
                "INSERT INTO project_meta SELECT singleton, application, 7, revision, host_epoch, created_at, updated_at FROM project_meta_v8"
            )
            connection.execute("DROP TABLE project_meta_v8")

    def test_populated_v7_preview_apply_resume_reverse_preserves_facts_and_exact_predecessor(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        path = fixture.work / "state.sqlite3"
        expected = fixture.store.validated_snapshot()
        self.v7(path)
        before = path.read_bytes()
        plan = schema_procedure.preview_schema(path)
        self.assertIsInstance(plan, schema_procedure.PlannedSchema)
        assert isinstance(plan, schema_procedure.PlannedSchema)
        self.assertIsInstance(plan.plan, schema_procedure.AdvancePlan)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse((fixture.work / "migration").exists())
        applied = schema_procedure.apply_schema(path, plan.plan_id)
        self.assertIsInstance(applied, schema_procedure.AppliedSchema)
        self.assertEqual(expected, fixture.store.validated_snapshot())
        resumed = schema_procedure.apply_schema(path, plan.plan_id)
        self.assertIsInstance(resumed, schema_procedure.AppliedSchema)
        assert isinstance(resumed, schema_procedure.AppliedSchema)
        self.assertEqual((), resumed.changed_surfaces)
        self.assertEqual(before, (fixture.work / "migration" / f"{plan.plan_id}.v7").read_bytes())
        self.assertEqual(expected.lifecycle, fixture.store.read_project_export_batches()[0].lifecycle)
        reverse = schema_procedure.preview_schema(path, plan.plan_id)
        assert isinstance(reverse, schema_procedure.PlannedSchema)
        self.assertIsInstance(schema_procedure.apply_schema(path, reverse.plan_id), schema_procedure.AppliedSchema)
        self.assertEqual(before, path.read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, reverse.plan_id), schema_procedure.AppliedSchema)
        self.assertIsInstance(schema_procedure.apply_schema(path, plan.plan_id), schema_procedure.AppliedSchema)
        self.assertEqual(expected, fixture.store.validated_snapshot())

    def test_v7_transaction_failure_rolls_back_and_stale_plan_refuses(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        path = fixture.work / "state.sqlite3"
        self.v7(path)
        before = path.read_bytes()
        plan = schema_procedure.preview_schema(path)
        assert isinstance(plan, schema_procedure.PlannedSchema)
        with patch(
            "pinboard.adapters.sqlite.database._verify_current_schema",
            side_effect=StorageError(StorageErrorCode.INVALID_STATE, "injected candidate schema failure"),
        ):
            failed = schema_procedure.apply_schema(path, plan.plan_id)
        self.assertIsInstance(failed, schema_procedure.SchemaProcedureFailure)
        self.assertEqual(before, path.read_bytes())
        self.assertIsInstance(schema_procedure.apply_schema(path, plan.plan_id), schema_procedure.AppliedSchema)
        operations = test_initiatives.InitiativeTest()
        operations.create(fixture)
        operations.mutate(fixture, "member-add", {"expected_revision": 1, "item_id": "work-a"})
        operations.mutate(
            fixture,
            "assess",
            {"expected_revision": 2, "evaluation_context": 1, "judgment": "met", "supporting_links": []},
        )
        self.assertEqual(1, len(fixture.store.validated_snapshot().initiatives))
        self.assertEqual(
            fixture.store.validated_snapshot().initiatives, fixture.store.read_project_export_batches()[0].initiatives
        )
        after_write = path.read_bytes()
        refused = schema_procedure.preview_schema(path, plan.plan_id)
        self.assertIsInstance(refused, schema_procedure.SchemaProcedureFailure)
        self.assertEqual(after_write, path.read_bytes())
        stale_fixture = self.checkpoint_fixture(local=True)
        stale_path = stale_fixture.work / "state.sqlite3"
        self.v7(stale_path)
        stale = schema_procedure.preview_schema(stale_path)
        assert isinstance(stale, schema_procedure.PlannedSchema)
        with contextlib.closing(sqlite3.connect(stale_path)) as connection, connection:
            connection.execute("UPDATE project_meta SET revision = revision + 1")
        changed = stale_path.read_bytes()
        self.assertIsInstance(
            schema_procedure.apply_schema(stale_path, stale.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertEqual(changed, stale_path.read_bytes())
        self.assertFalse((stale_fixture.work / "migration").exists())

    def test_retained_v6_plans_backups_and_predicted_v7_meaning_are_not_reinterpreted(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        path = fixture.work / "state.sqlite3"
        expected = fixture.store.validated_snapshot()
        test_storage_migration.StorageMigrationTests().downgrade_schema(path)
        predecessor = path.read_bytes()
        legacy = schema_procedure.preview_schema(path)
        assert isinstance(legacy, schema_procedure.PlannedSchema)
        self.assertIsInstance(legacy.plan, schema_procedure.CompatibilityForwardPlan)
        self.assertIsInstance(schema_procedure.apply_schema(path, legacy.plan_id), schema_procedure.AppliedSchema)
        legacy_plan_path = fixture.work / "migration" / f"{legacy.plan_id}.json"
        legacy_bytes = legacy_plan_path.read_bytes()
        self.assertEqual(7, database.inspect_schema_migration(path)[0])
        advance = schema_procedure.preview_schema(path)
        assert isinstance(advance, schema_procedure.PlannedSchema)
        self.assertIsInstance(schema_procedure.apply_schema(path, advance.plan_id), schema_procedure.AppliedSchema)
        self.assertEqual(expected, fixture.store.validated_snapshot())
        self.assertEqual(legacy_bytes, legacy_plan_path.read_bytes())
        self.assertEqual(predecessor, (fixture.work / "migration" / f"{legacy.plan_id}.v6").read_bytes())
        current = path.read_bytes()
        self.assertIsInstance(
            schema_procedure.apply_schema(path, legacy.plan_id), schema_procedure.SchemaProcedureFailure
        )
        self.assertEqual(current, path.read_bytes())
