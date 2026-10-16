"""Installed initiative facts, independent assessment context, and selected offline roadmap behavior."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

import msgspec

from pinboard.adapters.files import roadmap as roadmap_files
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.models import AffectedViews
from pinboard.adapters.sqlite import initiatives as initiative_storage
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import compact_queue, move_item_state_count
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import initiative_records, project_export, roadmap, stored_state
from pinboard.application import initiatives as initiative_application
from pinboard.cli import work_views
from pinboard.domain import initiatives
from pinboard.domain.identifiers import HostId, TaskId, WorkItemId
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_advertised_tool
from tests.support import SQLITE_NOW, JsonObject, JsonValue, work_root_durable


class InitiativeTest(CheckpointPackageSupport):
    def operation(self, fixture: CheckpointFixture, operation: str, fields: JsonObject) -> JsonObject:
        return call_advertised_tool(
            mcp_server.INITIATIVE_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": operation,
                    **fields,
                }
            },
        )

    def mutate(self, fixture: CheckpointFixture, operation: str, fields: JsonObject) -> JsonObject:
        return self.operation(
            fixture,
            operation,
            {
                "initiative_id": "workflow",
                "human": "Project owner",
                "date": "2026-10-16",
                "reason": "Make the observed workflow easier",
                "actor_task_id": "initiative-test",
                "actor_host_id": "local",
                **fields,
            },
        )

    def create(self, fixture: CheckpointFixture) -> JsonObject:
        result = self.mutate(
            fixture,
            "create",
            {
                "title": "A reliable workflow",
                "goal": "Finish the observed workflow",
                "rationale": "Connect existing work to its purpose",
                "success_signal": "The owner observes a complete workflow",
                "project_references": ["docs/vision.md"],
            },
        )
        self.assertEqual("committed", result["status"], result)
        return result

    def read(self, fixture: CheckpointFixture) -> initiatives.Initiative:
        value = SQLiteWorkStore(fixture.work / "state.sqlite3").read_initiative(initiatives.InitiativeId("workflow"))
        assert value is not None
        return value

    def revise(self, fixture: CheckpointFixture, definition: initiatives.Definition) -> None:
        result = self.mutate(
            fixture,
            "revise",
            {
                "expected_revision": self.read(fixture).subject_revision,
                "title": definition.title,
                "goal": definition.goal,
                "rationale": definition.rationale,
                "success_signal": definition.success_signal,
                "project_references": list[JsonValue](definition.project_references),
            },
        )
        self.assertEqual("committed", result["status"], result)

    def test_membership_decision_and_assessment_survive_application_reload_and_export(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        operations: tuple[tuple[str, JsonObject], ...] = (
            ("member-add", {"item_id": "work-a"}),
            (
                "decision",
                {"decision": "Retain the observed target", "supporting_links": ["https://example.com/evidence"]},
            ),
            ("assess", {"evaluation_context": 1, "judgment": "met", "supporting_links": ["session.txt"]}),
            ("member-remove", {"item_id": "work-a"}),
            ("member-add", {"item_id": "work-a"}),
        )
        for operation, fields in operations:
            result = self.mutate(
                fixture, operation, {"expected_revision": self.read(fixture).subject_revision, **fields}
            )
            self.assertEqual("committed", result["status"], result)
        value = self.read(fixture)
        self.assertEqual(6, value.subject_revision)
        self.assertEqual((1, 2), tuple(member.episode for member in value.memberships))
        self.assertIsNotNone(value.memberships[0].removed)
        self.assertIsNone(value.memberships[1].removed)
        self.assertEqual("Retain the observed target", value.decisions[0].decision)
        self.assertEqual(initiatives.Judgment.MET, value.assessments[0].judgment)
        self.assertEqual("2026-10-16", value.assessments[0].provenance.reason.date.isoformat())
        self.assertEqual("Project owner", value.assessments[0].provenance.reason.human)
        self.assertEqual("initiative-test", value.assessments[0].provenance.task_id)
        snapshot = fixture.store.validated_snapshot()
        self.assertEqual((value,), snapshot.initiatives)
        exported = fixture.store.read_project_export_batches()[0]
        self.assertEqual((value,), exported.initiatives)
        code, output, error = self.run_cli(*fixture.common, "export", "--json")
        self.assertEqual(0, code, error)
        package = msgspec.json.decode(output, type=project_export.ProjectExport)
        self.assertEqual((initiative_records.initiative_record(value),), package.initiatives)
        result = self.operation(fixture, "read", {"initiative_id": "workflow"})
        self.assertEqual("ok", result["status"], result)
        projected = self.operation(fixture, "roadmap", {})
        self.assertEqual("ok", projected["status"], projected)
        self.assertEqual({"delivered": 0, "unfinished": 4, "cancelled": 0, "superseded": 0}, projected["totals"])
        self.assertEqual(value.assessments[0].judgment, self.read(fixture).assessments[0].judgment)
        code, _, error = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(0, code, error)

    def test_relative_project_root_references_use_resolved_checkout_for_create_and_revise(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        nested = fixture.project / "docs"
        nested.mkdir()
        for operation, reference, revision in (("create", "docs/vision.md", None), ("revise", "docs/principles.md", 1)):
            with self.subTest(operation=operation), chdir(nested):
                fields: JsonObject = {
                    "project_root": ".",
                    "title": "Workflow",
                    "goal": "Goal",
                    "rationale": "Reason",
                    "success_signal": "Signal",
                    "project_references": [reference],
                }
                if revision is not None:
                    fields["expected_revision"] = revision
                result = self.mutate(fixture, operation, fields)
                self.assertEqual("committed", result["status"], result)
                target = str(fixture.project / reference)
                self.assertEqual((target,), self.read(fixture).revisions[-1].definition.project_references)
                self.assertIn(f'href="{target}"', (fixture.work / "views" / "roadmap.html").read_text())

    def test_goal_signal_changes_and_reversions_create_new_context_while_metadata_preserves_it(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        original = self.read(fixture).revisions[-1].definition
        assessed = self.mutate(
            fixture,
            "assess",
            {"expected_revision": 1, "evaluation_context": 1, "judgment": "not-met", "supporting_links": []},
        )
        self.assertEqual("committed", assessed["status"], assessed)
        self.revise(fixture, replace(original, title="Renamed workflow", rationale="More context"))
        self.assertEqual(1, self.read(fixture).revisions[-1].evaluation_context)
        for expected_context, definition in enumerate(
            (
                replace(original, goal="A different goal"),
                original,
                replace(original, success_signal="A different signal"),
                original,
            ),
            start=2,
        ):
            self.revise(fixture, definition)
            value = self.read(fixture)
            self.assertEqual(expected_context, value.revisions[-1].evaluation_context)
            self.assertFalse(any(assessment.evaluation_context == expected_context for assessment in value.assessments))
            self.assertEqual(original.goal, value.assessments[0].evaluated_goal)
            self.assertEqual(original.success_signal, value.assessments[0].evaluated_signal)
        before = fixture.store.validated_snapshot()
        refused = self.mutate(
            fixture,
            "assess",
            {
                "expected_revision": self.read(fixture).subject_revision,
                "evaluation_context": 1,
                "judgment": "met",
                "supporting_links": [],
            },
        )
        self.assertEqual("ASSESSMENT_CONTEXT_STALE", refused["code"], refused)
        self.assertEqual(before, fixture.store.validated_snapshot())
        page = (fixture.work / "views" / "roadmap.html").read_text()
        self.assertIn("Not assessed", page)
        self.assertIn(original.success_signal, page)

    def test_unchanged_refusals_strict_decode_and_failed_transaction(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        before = fixture.store.validated_snapshot()
        self.assertEqual("rejected", self.operation(fixture, "read", {"initiative_id": "absent"})["status"])
        self.assertEqual(
            "rejected", self.mutate(fixture, "member-add", {"expected_revision": 1, "item_id": "work-a"})["status"]
        )
        self.assertEqual("rejected", self.operation(fixture, "roadmap", {"extra": True})["status"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.create(fixture)
        refusals: tuple[tuple[str, JsonObject, str], ...] = (
            (
                "create",
                {
                    "title": "Duplicate",
                    "goal": "Goal",
                    "rationale": "Why",
                    "success_signal": "Signal",
                    "project_references": [],
                },
                "INITIATIVE_EXISTS",
            ),
            ("member-add", {"expected_revision": 99, "item_id": "work-a"}, "INITIATIVE_STALE"),
            ("member-add", {"expected_revision": 1, "item_id": "absent"}, "ITEM_NOT_FOUND"),
            ("member-remove", {"expected_revision": 1, "item_id": "work-a"}, "MEMBERSHIP_ABSENT"),
        )
        for operation, fields, code in refusals:
            before = fixture.store.validated_snapshot()
            result = self.mutate(fixture, operation, fields)
            self.assertEqual(code, result["code"], result)
            self.assertEqual(before, fixture.store.validated_snapshot())
        before = fixture.store.validated_snapshot()
        original_persist = initiative_storage.persist

        def fail_after_effect(
            connection: sqlite3.Connection,
            initiative_id: initiatives.InitiativeId,
            change: initiatives.Change,
            accepted: initiatives.Accepted,
            provenance: initiatives.Provenance,
        ) -> int:
            original_persist(connection, initiative_id, change, accepted, provenance)
            raise RuntimeError("injected transaction failure")

        with (
            patch("pinboard.adapters.sqlite.initiatives.persist", side_effect=fail_after_effect),
            self.assertRaisesRegex(RuntimeError, "injected transaction failure"),
            fixture.store.write() as transaction,
        ):
            provenance = initiatives.Provenance(
                initiatives.HumanReason("Owner", SQLITE_NOW.date(), "Reason"),
                TaskId("test-task"),
                HostId("test-host"),
                SQLITE_NOW,
                transaction.read_mutation_allocation().project_revision + 1,
            )
            transaction.commit_initiative(
                initiatives.InitiativeId("workflow"),
                initiatives.AddMember(1, WorkItemId("work-a")),
                initiatives.Accepted(2, 1),
                provenance,
            )
        self.assertEqual(before, fixture.store.validated_snapshot())
        result = self.mutate(fixture, "member-add", {"expected_revision": 1, "item_id": "work-a"})
        self.assertEqual("committed", result["status"], result)
        duplicate = self.mutate(fixture, "member-add", {"expected_revision": 2, "item_id": "work-a"})
        self.assertEqual("MEMBERSHIP_EXISTS", duplicate["code"], duplicate)

    def test_refresh_warning_preserves_committed_facts_and_explicit_rebuild_repairs(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.VIEW_REFRESH_FAILED, "roadmap.html unavailable"),
        ):
            result = self.mutate(
                fixture,
                "create",
                {
                    "title": "Workflow",
                    "goal": "Goal",
                    "rationale": "Reason",
                    "success_signal": "Signal",
                    "project_references": [],
                },
            )
        self.assertEqual("committed-with-warning", result["status"], result)
        self.assertEqual("do-not-retry", result["retry"])
        self.assertEqual(1, self.read(fixture).subject_revision)
        repaired = work_views.rebuild(work_root_durable(fixture.work), fixture.store, SQLITE_NOW)
        self.assertIsNone(repaired.warning)
        self.assertIn("snapshot revision", (fixture.work / "views" / "roadmap.html").read_text())

    def test_projection_read_failure_after_commit_reports_rebuild_without_fabricated_revision(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        with patch.object(
            SQLiteWorkStore,
            "read_generated_view_facts",
            side_effect=StorageError(StorageErrorCode.IO_ERROR, "projection read unavailable"),
        ):
            result = self.mutate(
                fixture,
                "create",
                {
                    "title": "Workflow",
                    "goal": "Goal",
                    "rationale": "Reason",
                    "success_signal": "Signal",
                    "project_references": [],
                },
            )
        self.assertEqual("committed-with-warning", result["status"], result)
        self.assertEqual("do-not-retry", result["retry"])
        self.assertEqual(1, self.read(fixture).subject_revision)
        self.assertIn(str(fixture.work), str(result["warning"]))
        self.assertIsNone(work_views.rebuild(work_root_durable(fixture.work), fixture.store, SQLITE_NOW).warning)

    def test_full_validation_and_export_reject_damaged_evaluation_context(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        with fixture.store.write() as transaction:
            transaction.connection.execute(
                "UPDATE initiative_contexts SET goal = 'another goal' WHERE initiative_id = 'workflow'"
            )
        for read in (fixture.store.validated_snapshot, fixture.store.read_project_export_batches):
            with self.assertRaises(StorageError) as damaged:
                read()
            self.assertEqual(StorageErrorCode.INVALID_STATE, damaged.exception.code)

    def test_full_validation_and_export_reject_conflicting_decision_and_assessment_commit_revisions(self) -> None:
        cases: tuple[tuple[str, JsonObject, JsonObject, str], ...] = (
            (
                "decision",
                {"decision": "Keep the target", "supporting_links": []},
                {"decision": "Revise the target", "supporting_links": []},
                "UPDATE initiative_decisions SET project_revision = 999999999 WHERE project_revision = ?",
            ),
            (
                "assess",
                {"evaluation_context": 1, "judgment": "met", "supporting_links": []},
                {"evaluation_context": 1, "judgment": "not-met", "supporting_links": []},
                "UPDATE initiative_assessments SET project_revision = 999999999 WHERE project_revision = ?",
            ),
        )
        for operation, first, second, damage in cases:
            with self.subTest(operation=operation):
                fixture = self.checkpoint_fixture(local=True)
                self.create(fixture)
                result = self.mutate(fixture, operation, {"expected_revision": 1, **first})
                self.assertEqual("committed", result["status"], result)
                first_revision = fixture.store.validated_snapshot().lifecycle.project.revision
                result = self.mutate(fixture, operation, {"expected_revision": 2, **second})
                self.assertEqual("committed", result["status"], result)
                with fixture.store.write() as transaction:
                    transaction.connection.execute(damage, (first_revision,))
                for read in (fixture.store.validated_snapshot, fixture.store.read_project_export_batches):
                    with self.assertRaises(StorageError) as damaged:
                        read()
                    self.assertEqual(StorageErrorCode.INVALID_STATE, damaged.exception.code)
                    self.assertIn("workflow", str(damaged.exception))

    def test_full_validation_rejects_definition_revision_outside_subject_history(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        with fixture.store.write() as transaction:
            transaction.connection.execute(
                "UPDATE initiative_revisions SET revision = 999 WHERE initiative_id = 'workflow'"
            )
        with self.assertRaises(StorageError):
            fixture.store.validated_snapshot()

    def test_malformed_link_remains_safe_text_without_losing_committed_assessment(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        result = self.mutate(
            fixture,
            "assess",
            {"expected_revision": 1, "evaluation_context": 1, "judgment": "met", "supporting_links": ["http://["]},
        )
        self.assertEqual("committed", result["status"], result)
        self.assertIn("http://[", (fixture.work / "views" / "roadmap.html").read_text())
        self.assertEqual(("http://[",), self.read(fixture).assessments[0].supporting_links)

    def test_offline_text_escaping_complete_history_and_selected_archive_scope(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        hostile = '</script><img src=x onerror="alert(1)"> & café'
        self.mutate(
            fixture,
            "create",
            {
                "title": hostile,
                "goal": hostile,
                "rationale": hostile,
                "success_signal": hostile,
                "project_references": ["javascript:alert(1)"],
            },
        )
        self.mutate(fixture, "member-add", {"expected_revision": 1, "item_id": "work-a"})
        self.mutate(fixture, "member-remove", {"expected_revision": 2, "item_id": "work-a", "reason": hostile})
        projection = fixture.store.read_roadmap()
        markdown, html = roadmap_files.render(projection)
        self.assertIn(hostile.encode(), markdown)
        self.assertNotIn(b"<img", html)
        self.assertNotIn(b'href="javascript:', html)
        self.assertIn(b"&lt;img", html)
        self.assertIn(b"removed:", html)
        self.assertNotIn(b"<script src=", html)
        self.assertIn(b"width=device-width", html)
        with patch("pinboard.adapters.files.views.datetime"):
            self.assertEqual((markdown, html), roadmap_files.render(projection))
        with fixture.store.write() as transaction:
            transaction.connection.executemany(
                "INSERT INTO work_items VALUES (?, 'done', NULL, NULL, 'unrelated archive', NULL, NULL, 0, ?, ?, NULL)",
                (
                    (f"unrelated-archive-{index}", SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat())
                    for index in range(1000)
                ),
            )
        self.assertEqual(projection, fixture.store.read_roadmap())
        self.assertNotIn(b"unrelated-archive", roadmap_files.render(fixture.store.read_roadmap())[1])

    def test_shared_terminal_member_removal_and_readdition_use_single_item_identity(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        self.mutate(
            fixture,
            "create",
            {
                "initiative_id": "controls",
                "title": "Controls",
                "goal": "Control targets",
                "rationale": "Shared discovery",
                "success_signal": "Observed controls",
                "project_references": [],
            },
        )
        for initiative_id in ("workflow", "controls"):
            result = self.mutate(
                fixture, "member-add", {"initiative_id": initiative_id, "expected_revision": 1, "item_id": "work-a"}
            )
            self.assertEqual("committed", result["status"], result)
        # Simulate the existing item's successful terminal disposition; initiative writes never own it.
        removed_position = next(
            item.queue_position
            for item in fixture.store.validated_snapshot().lifecycle.work_items
            if item.item_id == "work-a"
        )
        assert removed_position is not None
        with fixture.store.write() as transaction:
            connection = transaction.connection
            connection.execute(
                "UPDATE work_items SET state = 'done', outcome_evidence = 'successful fixture completion', queue_position = NULL WHERE item_id = 'work-a'"
            )
            connection.execute("UPDATE attempts SET state = 'done' WHERE attempt_id = 'work-a-1'")
            self.assertIsNone(compact_queue(connection, removed_position))
            move_item_state_count(
                connection, stored_state.StoredWorkItemState.REVIEW, stored_state.StoredWorkItemState.DONE
            )
        value = SQLiteWorkStore(fixture.work / "state.sqlite3").read_roadmap()
        self.assertEqual(1, value.totals.delivered)
        self.assertEqual(2, roadmap_files.render(value)[0].count(b"](items/work-a.md)"))
        self.assertEqual((), next(item.dependencies for item in value.facts.items if item.item_id == "work-a"))
        self.assertEqual(value, roadmap.from_state(fixture.store.validated_snapshot()))
        code, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, code, (output, error))
        code, output, error = self.run_cli(*fixture.common, "validate", "--json")
        self.assertEqual(0, code, (output, error))
        self.assertTrue(all(not initiative.assessments for initiative in value.facts.initiatives))
        self.assertEqual(
            "committed", self.mutate(fixture, "member-remove", {"expected_revision": 2, "item_id": "work-a"})["status"]
        )
        self.assertEqual(1, fixture.store.read_roadmap().totals.delivered)
        self.assertEqual(
            "committed", self.mutate(fixture, "member-add", {"expected_revision": 3, "item_id": "work-a"})["status"]
        )
        self.assertEqual(1, fixture.store.read_roadmap().totals.delivered)
        self.assertEqual(
            "committed", self.mutate(fixture, "member-add", {"expected_revision": 4, "item_id": "work-b"})["status"]
        )
        reloaded = fixture.store.read_roadmap()
        self.assertEqual((1, 1), (reloaded.totals.delivered, reloaded.totals.superseded))
        self.assertEqual(3, len(self.read(fixture).memberships))
        self.assertIsNotNone(self.read(fixture).memberships[0].removed)

    def test_concurrent_stale_initiative_changes_reject_without_partial_effect(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        barrier = Barrier(2)

        def add(item_id: str) -> int | initiatives.Rejected:
            barrier.wait(5)
            return initiative_application.change(
                SQLiteWorkStore(fixture.work / "state.sqlite3"),
                initiatives.InitiativeId("workflow"),
                initiatives.AddMember(1, WorkItemId(item_id)),
                initiatives.HumanReason("Owner", SQLITE_NOW.date(), "Shared discovery"),
                TaskId(item_id),
                HostId("local"),
                SQLITE_NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(add, item_id) for item_id in ("work-a", "work-c")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(1, sum(isinstance(result, int) for result in results))
        self.assertEqual(
            [initiatives.RejectionCode.INITIATIVE_STALE],
            [result.code for result in results if isinstance(result, initiatives.Rejected)],
        )
        value = self.read(fixture)
        self.assertEqual((2, 1), (value.subject_revision, len(value.memberships)))
        self.assertEqual((value,), fixture.store.validated_snapshot().initiatives)

    def test_older_refresh_cannot_overwrite_newer_snapshot(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        old_revision = fixture.store.read_roadmap().facts.project_revision
        snapshot_read, release, second_started = Event(), Event(), Event()
        real_render = roadmap_files.render

        def render(value: roadmap.Projection) -> tuple[bytes, bytes]:
            if value.facts.project_revision == old_revision:
                snapshot_read.set()
                if not release.wait(10):
                    raise AssertionError("Old refresh was never released")
            return real_render(value)

        def refresh(second: bool) -> None:
            if second:
                second_started.set()
            result = work_views.refresh(
                work_root_durable(fixture.work), fixture.store, AffectedViews((), (), ()), SQLITE_NOW
            )
            self.assertIsNone(result.warning)

        with (
            patch("pinboard.adapters.files.roadmap.render", side_effect=render),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(refresh, False)
            try:
                self.assertTrue(snapshot_read.wait(10))
                changed = initiative_application.change(
                    fixture.store,
                    initiatives.InitiativeId("workflow"),
                    initiatives.RecordDecision(1, "Keep the target", ()),
                    initiatives.HumanReason("Owner", SQLITE_NOW.date(), "Observed evidence"),
                    TaskId("test"),
                    HostId("local"),
                    SQLITE_NOW,
                )
                self.assertIsInstance(changed, int)
                second = executor.submit(refresh, True)
                self.assertTrue(second_started.wait(10))
            finally:
                release.set()
            first.result(timeout=10)
            second.result(timeout=10)
        page = (fixture.work / "views" / "roadmap.html").read_text()
        self.assertIn(f"snapshot revision {changed}", page)
        self.assertIn("Keep the target", page)
        self.assertNotIn(f"snapshot revision {old_revision}<", page)

    def test_roadmap_queries_are_batched_and_use_selected_indexes(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture)
        self.mutate(fixture, "member-add", {"expected_revision": 1, "item_id": "work-a"})
        statements: list[str] = []
        real_open = sqlite_store.open_database

        def opened(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = real_open(path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("pinboard.adapters.sqlite.store.open_database", side_effect=opened):
            fixture.store.read_roadmap()
        baseline = len(statements)
        with fixture.store.write() as transaction:
            connection = transaction.connection
            connection.executemany(
                "INSERT INTO work_items VALUES (?, 'done', NULL, NULL, 'unrelated archive', NULL, NULL, 0, ?, ?, NULL)",
                ((f"query-archive-{index}", SQLITE_NOW.isoformat(), SQLITE_NOW.isoformat()) for index in range(1000)),
            )
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT item_id FROM initiative_memberships WHERE initiative_id = ? AND removed_json IS NULL",
                ("workflow",),
            ).fetchall()
            self.assertTrue(any("initiative_active_member" in str(tuple(row)) for row in plan))
        statements.clear()
        with patch("pinboard.adapters.sqlite.store.open_database", side_effect=opened):
            value = fixture.store.read_roadmap()
        self.assertEqual(baseline, len(statements))
        self.assertFalse(any(item.item_id.startswith("query-archive-") for item in value.facts.items))
        self.assertLess(baseline, 25)
