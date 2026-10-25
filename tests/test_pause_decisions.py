"""A native decision batch crosses pause persistence, fresh reads and exact human resolution."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import UnexpectedToolError

from pinboard.adapters.sqlite import database, schema_procedure
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.decision_batches import read_decision_batches
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, stored_state
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.mcp import server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import native_tools
from tests.support import JsonObject


def pause_payload() -> JsonObject:
    return {
        "schema": "pinboard-pause-decisions/v1",
        "reason": "Two choices need the human.",
        "decisions": [
            {
                "decision_id": "input-policy",
                "question": "Which input policy?",
                "options": ["Reject", "Replace"],
                "recommendation": "Reject",
                "unblocks": "Input handling",
                "deferral_cost": "Input work waits.",
            },
            {
                "decision_id": "queue-placement",
                "question": "Where should it go?",
                "options": ["Front", "Back"],
                "recommendation": "Back",
                "unblocks": "Queue placement",
                "deferral_cost": "Placement waits.",
            },
        ],
    }


class PauseDecisionsTest(CheckpointPackageSupport):
    def test_pending_batch_rejects_partial_duplicate_unknown_and_dependency_bypasses(self) -> None:
        native = self.enterContext(native_tools())
        fixture = replace(
            self.active_fixture(),
            call_native_tool=native.call_advertised_tool,
            call_advertised_tool=native.call_advertised_tool,
        )
        self.assertEqual(
            "committed",
            self.transition_result(fixture, self.project_action(fixture, "pause:work-a-1"), pause_payload())["status"],
        )
        answers: JsonObject = {
            "schema": "pinboard-resolve-pause-decisions/v1",
            "brief_artifact_ref_id": None,
            "resolutions": [
                {"kind": "answer", "decision_id": "input-policy", "human_decision": "Reject."},
                {
                    "kind": "defer",
                    "decision_id": "queue-placement",
                    "human_decision": "Defer.",
                    "reopen_condition": "After input handling ships.",
                },
            ],
        }
        before = fixture.store.validated_snapshot()
        selected = fixture.store.read_item_status(WorkItemId("work-a"))
        assert selected is not None
        unavailable = fixture.call_advertised_tool(
            server.TRANSITION_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "role": "project",
                    "receipt": {
                        "action_id": {"kind": "resume", "subject": "work-a"},
                        "subject_revision": str(selected.work_item.subject_revision),
                    },
                    "payload": answers,
                    "actor_task_id": "review-owner",
                    "actor_host_id": "local",
                }
            },
        )
        self.assertEqual("ACTION_NOT_AVAILABLE", unavailable["code"], unavailable)
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual(
            0,
            self.run_cli(
                *fixture.common,
                "close",
                "work-c",
                "--outcome",
                "done",
                "--reason",
                "Prerequisite done.",
                "--task-id",
                "review-owner",
                "--host-id",
                "local",
            )[0],
        )
        action = self.project_action(fixture, "resume:work-a")
        before = fixture.store.validated_snapshot()
        for condition in ("partial", "duplicate", "unknown", "missing-condition", "missing-words"):
            with self.subTest(condition=condition):
                malformed = deepcopy(answers)
                rows = self.json_array(malformed["resolutions"])
                if condition == "partial":
                    rows.pop()
                elif condition == "duplicate":
                    rows.append(rows[0])
                elif condition == "unknown":
                    rows.append({"kind": "answer", "decision_id": "unrelated", "human_decision": "Unknown."})
                elif condition == "missing-condition":
                    self.json_object(rows[1]).pop("reopen_condition")
                else:
                    self.json_object(rows[0]).pop("human_decision")
                rejected = self.transition_result(fixture, action, malformed)
                self.assertEqual("TRANSITION_INPUT_INVALID", rejected["code"], rejected)
                if condition == "partial":
                    self.assertIn("queue-placement", str(rejected["message"]))
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_batch_receipt_and_reference_roll_back_together(self) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        before = fixture.store.validated_snapshot()
        append = sqlite_state.append_history

        def append_then_fail(
            connection: sqlite3.Connection, records: tuple[stored_state.StoredTransitionReceipt, ...]
        ) -> None:
            append(connection, records)
            raise RuntimeError("Controlled failure after receipt and reference writes.")

        with (
            patch.object(sqlite_state, "append_history", side_effect=append_then_fail),
            self.assertRaises(UnexpectedToolError),
        ):
            self.transition_result(fixture, action, pause_payload())
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_selected_batch_read_is_indexed_and_full_validation_finds_missing_projection(self) -> None:
        fixture = self.active_fixture()
        self.assertEqual(
            "committed",
            self.transition_result(fixture, self.project_action(fixture, "pause:work-a-1"), pause_payload())["status"],
        )
        before = fixture.store.validated_snapshot()
        with closing(database.open_database(fixture.work / "state.sqlite3", OpenMode.READ_WRITE)) as connection:
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            expected = read_decision_batches(connection, (AttemptId("work-a-1"),))
            selected_count = len(statements)
            connection.execute("BEGIN")
            for index in range(40):
                connection.execute(
                    """INSERT INTO transition_history(history_id, project_revision, action_id, action_kind,
                    subject_id, authorization_kind, input_schema, input_json, outcome_schema, outcome_json, committed_at)
                    SELECT ?, ?, ?, action_kind, 'unrelated-attempt', authorization_kind, 'decision/v1', '{}', outcome_schema,
                    outcome_json, committed_at FROM transition_history WHERE history_id = ?""",
                    (
                        100000 + index,
                        100000 + index,
                        f"unrelated-{index}",
                        expected[AttemptId("work-a-1")].pause_history_id,
                    ),
                )
            statements.clear()
            self.assertEqual(expected, read_decision_batches(connection, (AttemptId("work-a-1"),)))
            selected_statements = tuple(statements)
            self.assertEqual(selected_count, len(selected_statements))
            connection.set_trace_callback(None)
            for statement in selected_statements:
                details = [str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")]
                self.assertTrue(any("SEARCH " in detail for detail in details), details)
                self.assertFalse(any("SCAN " in detail for detail in details), details)
            connection.rollback()
            connection.execute("BEGIN")
            connection.execute("DELETE FROM attempt_decision_batches")
            with self.assertRaises(StorageError):
                sqlite_state.read_state(connection)
            with self.assertRaises(StorageError):
                sqlite_state.read_project_export_state(connection)
            connection.rollback()
            connection.execute("BEGIN")
            connection.execute(
                "UPDATE transition_history SET input_json = '{}' WHERE input_schema = 'pinboard-pause-decisions/v1'"
            )
            with self.assertRaises(StorageError):
                read_decision_batches(connection, (AttemptId("work-a-1"),))
            connection.rollback()
        self.assertEqual(before, fixture.store.validated_snapshot())

    def assert_current_batch(self, fixture: CheckpointFixture, question: str, answer: str | None) -> None:
        roots = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        inspected = fixture.call_advertised_tool(
            server.ATTEMPT_INSPECT_TOOL, {**roots, "attempt_id": "work-a-1", "reconciliation": None}
        )
        batch = self.json_object(self.json_object(inspected["continuation"])["decision_batch"])
        self.assertEqual(question, self.json_object(self.json_array(batch["decisions"])[0])["question"])
        for name in ("items/work-a.md", "board.md", "board.html"):
            text = (fixture.work / "views" / name).read_text()
            self.assertIn(question, text, name)
            self.assertIn("Input work waits.", text, name)
            if answer is not None:
                self.assertIn(answer.splitlines()[0], text, name)
        validated, stdout, stderr = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(0, validated, f"{stdout}\n{stderr}")

    def test_v10_preview_apply_reload_export_validate_and_exact_reversal(self) -> None:  # noqa: PLR0915 - one persisted migration, refresh and reversal journey
        fixture = self.checkpoint_fixture()
        path = fixture.work / "state.sqlite3"
        expected = fixture.store.validated_snapshot()
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
        markdown_views = tuple(
            view for view in (fixture.work / "views").rglob("*.md") if "authority: sqlite-v11" in view.read_text()
        )
        self.assertTrue(markdown_views)
        for view in markdown_views:
            current = view.read_text()
            self.assertIn("authority: sqlite-v11", current)
            stale = current.replace("authority: sqlite-v11", "authority: sqlite-v10")
            view.write_text(stale)
            self.assertEqual(stale, view.read_text())
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE attempt_decision_batches")
            connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v11")
            statement = next(
                sql for _, name, _, sql in database._build_v10_schema_signature() if name == "project_meta"
            )
            assert statement is not None
            connection.execute(statement)
            connection.execute(
                "INSERT INTO project_meta SELECT singleton, application, 10, revision, host_epoch, created_at, updated_at FROM project_meta_v11"
            )
            connection.execute("DROP TABLE project_meta_v11")
            database._verify_v10_schema(connection)
        original = path.read_bytes()
        with self.assertRaises(StorageError) as refused:
            database.open_database(path, OpenMode.READ_ONLY)
        self.assertEqual(StorageErrorCode.SCHEMA_UNSUPPORTED, refused.exception.code)
        self.assertEqual(original, path.read_bytes())
        preview = self.run_json_cli(*fixture.common, "migrate-schema")
        plan_id = str(preview["plan_id"])
        self.assertEqual("decisions-advance", self.json_object(preview["plan"])["kind"])
        self.assertEqual(original, path.read_bytes())
        verify = database._verify_current_schema

        def fail_before_commit(connection: sqlite3.Connection) -> None:
            verify(connection)
            if connection.execute("PRAGMA database_list").fetchone()[2] == str(path):
                raise StorageError(StorageErrorCode.INVALID_STATE, "Controlled pre-commit failure.")

        with patch.object(database, "_verify_current_schema", side_effect=fail_before_commit):
            failed = schema_procedure.apply_schema(path, plan_id)
        self.assertIsInstance(failed, schema_procedure.SchemaProcedureFailure)
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(original, (fixture.work / "migration" / f"{plan_id}.v10").read_bytes())
        migrated = self.run_json_cli(*fixture.common, "migrate-schema", "--apply", plan_id)
        self.assertEqual("sqlite-v11", migrated["authority"])
        self.assertIsNone(migrated["warning"])
        for view in markdown_views:
            self.assertIn("authority: sqlite-v11", view.read_text())
            self.assertNotIn("authority: sqlite-v10", view.read_text())
        self.assertEqual(expected, SQLiteWorkStore(path).validated_snapshot())
        exported = self.run_json_cli(*fixture.common, "export")
        self.assertEqual(11, self.json_object(exported["project"])["schema_version"])
        validated = self.run_json_cli(*fixture.common, "validate")
        self.assertTrue(validated["valid"])
        self.assertNotIn(
            "VIEW_REFRESH_REQUIRED",
            [self.json_object(value)["code"] for value in self.json_array(validated["diagnostics"])],
        )
        reverse = self.run_json_cli(*fixture.common, "migrate-schema", "--reverse", plan_id)
        reversed_result = self.run_json_cli(*fixture.common, "migrate-schema", "--apply", str(reverse["plan_id"]))
        self.assertEqual("sqlite-v10", reversed_result["authority"])
        self.assertIsNone(reversed_result["warning"])
        self.assertEqual(original, path.read_bytes())
        self.run_json_cli(*fixture.common, "migrate-schema", "--apply", plan_id)
        changed = self.transition_result(
            fixture,
            self.project_action(fixture, "return-for-correction:work-a-1"),
            {"reason": "A later supported write."},
        )
        self.assertEqual("committed", changed["status"], changed)
        later = path.read_bytes()
        self.assertIsInstance(schema_procedure.preview_schema(path, plan_id), schema_procedure.SchemaProcedureFailure)
        self.assertEqual(later, path.read_bytes())

    def test_native_pause_reload_refusal_and_attributed_resolution(self) -> None:  # noqa: PLR0915 - one persisted two-batch journey
        native = self.enterContext(native_tools())
        fixture = replace(
            self.active_fixture(),
            call_native_tool=native.call_advertised_tool,
            call_advertised_tool=native.call_advertised_tool,
        )
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The prerequisite is satisfied.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")
        payload = pause_payload()
        paused = self.transition_result(fixture, self.project_action(fixture, "pause:work-a-1"), payload)
        self.assertEqual("committed", paused["status"], paused)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        context = fresh.read_item_status(WorkItemId("work-a"))
        assert context is not None
        batch = context.batches_by_attempt[next(iter(context.batches_by_attempt))]
        self.assertEqual(["input-policy", "queue-placement"], [row.decision_id for row in batch.decisions])
        self.assertIsNone(batch.resolution)
        self.assert_current_batch(fixture, "Which input policy?", None)
        before = fresh.validated_snapshot()
        resume = self.project_action(fixture, "resume:work-a")
        refused = self.transition_result(fixture, resume, {})
        self.assertEqual("TRANSITION_INPUT_INVALID", refused["code"], refused)
        self.assertIn("input-policy", str(refused["message"]))
        self.assertEqual(before, fresh.validated_snapshot())
        words = "Reject it exactly as supplied.\nKeep the human's words."
        resumed = self.transition_result(
            fixture,
            resume,
            {
                "schema": "pinboard-resolve-pause-decisions/v1",
                "brief_artifact_ref_id": None,
                "resolutions": [
                    {"kind": "answer", "decision_id": "input-policy", "human_decision": words},
                    {
                        "kind": "defer",
                        "decision_id": "queue-placement",
                        "human_decision": "Defer placement.",
                        "reopen_condition": "Reopen when the next queue item is chosen.",
                    },
                ],
            },
        )
        self.assertEqual("committed", resumed["status"], resumed)
        status = fixture.call_advertised_tool(
            server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "item_id": "work-a",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                }
            },
        )
        attempt = self.json_object(self.json_array(status["attempts"])[0])
        resolved = self.json_object(self.json_object(attempt["decision_batch"])["resolution"])
        self.assertEqual(words, self.json_object(self.json_array(resolved["resolutions"])[0])["human_decision"])
        self.assertEqual("review-owner", resolved["actor_task_id"])
        self.assertEqual("local", resolved["actor_host_id"])
        self.assert_current_batch(fixture, "Which input policy?", words)
        plain = self.transition_result(
            fixture, self.project_action(fixture, "pause:work-a-1"), {"reason": "A plain pause."}
        )
        self.assertEqual("committed", plain["status"], plain)
        self.assert_current_batch(fixture, "Which input policy?", words)
        rebind = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": fixture.brief.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": int(fresh.validated_snapshot().lifecycle.attempts[0].brief_artifact_ref_id),
            },
        )
        self.assertEqual("committed", rebind["status"], rebind)
        self.assert_current_batch(fixture, "Which input policy?", words)
        plain_resume = self.transition_result(fixture, self.project_action(fixture, "resume:work-a"), {})
        self.assertEqual("committed", plain_resume["status"], plain_resume)
        second = pause_payload()
        decisions = self.json_array(second["decisions"])
        self.json_object(decisions[0])["question"] = "Which input policy now?"
        self.assertEqual(
            "committed",
            self.transition_result(fixture, self.project_action(fixture, "pause:work-a-1"), second)["status"],
        )
        self.assert_current_batch(fixture, "Which input policy now?", None)
        before = fresh.validated_snapshot()
        stale = self.transition_result(fixture, resume, {})
        self.assertEqual("rejected", stale["status"], stale)
        self.assertEqual(before, fresh.validated_snapshot())
        exported = self.run_json_cli(*fixture.common, "export")
        transitions = [self.json_object(row) for row in self.json_array(exported["transitions"])]
        pauses = [row for row in transitions if row["input_schema"] == "pinboard-pause-decisions/v1"]
        self.assertEqual(2, len(pauses))
        self.assertEqual(
            "Which input policy?",
            self.json_object(self.json_array(self.json_object(pauses[0]["input"])["decisions"])[0])["question"],
        )
        history_text = (fixture.work / "views" / "history" / f"{batch.pause_history_id}.md").read_text()
        self.assertIn("Which input policy?", history_text)
        self.assertIn("Reject", history_text)

    def test_strict_batches_and_own_worker_pause_preserve_independent_inputs(self) -> None:
        native = self.enterContext(native_tools())
        fixture = replace(
            self.active_fixture(),
            call_native_tool=native.call_advertised_tool,
            call_advertised_tool=native.call_advertised_tool,
        )
        action = self.project_action(fixture, "pause:work-a-1")
        before = fixture.store.validated_snapshot()
        for condition in ("missing", "duplicate-id", "invalid-id", "duplicate-option", "recommendation", "unknown"):
            with self.subTest(condition=condition):
                malformed = deepcopy(pause_payload())
                decisions = self.json_array(malformed["decisions"])
                first = self.json_object(decisions[0])
                if condition == "missing":
                    first.pop("question")
                elif condition == "duplicate-id":
                    self.json_object(decisions[1])["decision_id"] = first["decision_id"]
                elif condition == "invalid-id":
                    first["decision_id"] = "Invalid ID"
                elif condition == "duplicate-option":
                    first["options"] = ["Reject", "Reject"]
                elif condition == "recommendation":
                    first["recommendation"] = "Unknown option"
                else:
                    malformed["extra"] = True
                rejected = self.transition_result(fixture, action, malformed)
                self.assertEqual("TRANSITION_INPUT_INVALID", rejected["code"], rejected)
                self.assertEqual(before, fixture.store.validated_snapshot())
        lease = self.native_attempt_acquire(fixture, "batch-worker")
        own = self.native_actions(fixture, "pause", "work-a-1", role="worker", lease=lease)
        paused = self.transition_result(fixture, own, pause_payload())
        self.assertEqual("committed", paused["status"], paused)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(fresh, query_models.NonterminalAttemptContextFacts) and fresh.decision_batch is not None
        self.assertEqual("batch-worker", fresh.decision_batch.actor_task_id)
        other = self.checkpoint_fixture()
        before = other.store.validated_snapshot()
        rejected = self.transition_result(
            other, self.project_action(other, "return-for-correction:work-a-1"), pause_payload()
        )
        self.assertEqual("TRANSITION_INPUT_INVALID", rejected["code"], rejected)
        self.assertEqual(before, other.store.validated_snapshot())
