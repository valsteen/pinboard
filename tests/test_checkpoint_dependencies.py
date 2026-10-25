import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.dependency_operations import resolve_dependency_brief_pins
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import dependencies as sqlite_dependencies
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import proposal_models, proposals, roadmap, service
from pinboard.domain import history, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import CheckpointId, HostId, TaskId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.domain_support import expect_success
from tests.support import JsonObject
from tests.test_proposals import proposal
from tests.test_work_item_definitions import definition


class CheckpointDependencyContractTest(unittest.TestCase):
    def test_structured_target_roundtrip_and_duplicate_producer_rejection(self) -> None:
        target = work_models.CheckpointDependency(WorkItemId("campaign"), CheckpointId("survey"))
        value = replace(definition(), dependencies=(target, WorkItemId("other")))
        encoded = expect_success(history.work_item_definition_bytes(value))

        self.assertEqual(value, expect_success(history.decode_work_item_definition(encoded)))
        self.assertEqual(
            [{"item": "campaign", "checkpoint": "survey"}, "other"],
            msgspec.json.decode(encoded)["dependencies"],
        )
        self.assertIsInstance(
            history.work_item_definition_bytes(replace(value, dependencies=(target, target.item_id))),
            DecisionFailure,
        )
        for malformed in (
            {"item": "campaign", "checkpoint": "bad id"},
            {"item": "campaign", "checkpoint": "survey", "extra": True},
        ):
            with self.subTest(malformed=malformed), self.assertRaises(msgspec.ValidationError):
                msgspec.convert(malformed, type=history.CheckpointDependencyPayload)
        invalid = work_models.CheckpointDependency(WorkItemId("bad id"), CheckpointId("survey"))
        self.assertIsInstance(
            history.work_item_definition_bytes(replace(value, dependencies=(invalid,))), DecisionFailure
        )

    def test_acceptance_releases_only_matching_condition_on_live_or_dropped_producer(self) -> None:
        target = work_models.CheckpointDependency(WorkItemId("campaign"), CheckpointId("survey"))
        other = work_models.CheckpointDependency(target.item_id, CheckpointId("delivery"))
        snapshot = LedgerSnapshot(
            "1",
            (),
            dependency_facts=(),
            dropped_item_ids=frozenset((target.item_id,)),
            accepted_checkpoints=frozenset((target,)),
        )

        self.assertEqual((other, target.item_id), snapshot.unsatisfied_dependencies((target, other, target.item_id)))
        self.assertEqual(
            (),
            replace(snapshot, dropped_item_ids=frozenset()).unsatisfied_dependencies((target, other, target.item_id)),
        )


class InstalledCheckpointDependencyTest(CheckpointPackageSupport):
    def create(self, fixture: CheckpointFixture, item_id: str, target: str | JsonObject) -> JsonObject:
        value = proposal()
        value.update(proposal_id=item_id, relation={"kind": "follow-up", "item": target})
        return fixture.call_advertised_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "proposal": value,
                "actor_task_id": "coordinator",
                "actor_host_id": "local",
            },
        )

    def overview(self, fixture: CheckpointFixture) -> dict[str, JsonObject]:
        value = fixture.call_advertised_tool(
            mcp_server.OVERVIEW_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
            },
        )
        return {str(row["item_id"]): row for raw in self.json_array(value["items"]) if (row := self.json_object(raw))}

    def test_authoring_acceptance_fresh_reload_views_export_and_cancellation_agree(self) -> None:  # noqa: PLR0915 - one installed dependency lifecycle
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        target: JsonObject = {"item": "work-a", "checkpoint": checkpoint}
        before = fixture.store.validated_snapshot()
        invalid = self.create(fixture, "unknown-checkpoint", {**target, "checkpoint": "invented"})
        self.assertEqual(("rejected", "ITEM_DEFINITION_INVALID"), (invalid["status"], invalid["code"]))
        self.assertIn(f"Known current checkpoint IDs: {checkpoint}", str(invalid["message"]))
        self.assertEqual(before, fixture.store.validated_snapshot())

        for item_id, dependency in (("after-checkpoint", target), ("after-campaign", "work-a")):
            created = self.create(fixture, item_id, dependency)
            self.assertEqual("committed", created["status"], created)
        initial = self.overview(fixture)
        self.assertFalse(initial["after-checkpoint"]["eligible"])
        self.assertEqual([target], initial["after-checkpoint"]["depends_on"])
        self.assertFalse(initial["after-campaign"]["eligible"])

        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        accepted = self.transition_result(fixture, action, self.json_object(json.loads(fixture.payload.read_bytes())))
        self.assertEqual("committed", accepted["status"], accepted)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        state = fresh.validated_snapshot()
        self.assertEqual(1, len(state.lifecycle.checkpoint_acceptances))
        self.assertEqual(WorkItemId("work-a"), state.lifecycle.checkpoint_acceptances[0].item_id)
        links = {value.item_id: value for value in state.lifecycle.dependencies if value.item_id.startswith("after-")}
        self.assertEqual(CheckpointId(checkpoint), links[WorkItemId("after-checkpoint")].checkpoint_id)
        self.assertIsNone(links[WorkItemId("after-campaign")].checkpoint_id)

        current = self.overview(fixture)
        self.assertTrue(current["after-checkpoint"]["eligible"])
        self.assertFalse(current["after-campaign"]["eligible"])
        reasons = self.json_array(current["after-checkpoint"]["dependency_reasons"])
        self.assertTrue(self.json_object(reasons[0])["checkpoint_accepted"])
        status = fixture.call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "item_id": "after-checkpoint",
                }
            },
        )
        self.assertEqual([{"target": target, "satisfied": True, "checkpoint_accepted": True}], status["dependencies"])
        preview = fixture.call_advertised_tool(
            mcp_server.PARALLEL_PREVIEW_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "selection": "selected",
                    "item_ids": ["after-checkpoint", "after-campaign"],
                }
            },
        )
        self.assertEqual("after-checkpoint", self.json_object(self.json_array(preview["launchable"])[0])["item_id"])
        self.assertEqual("after-campaign", self.json_object(self.json_array(preview["excluded"])[0])["item_id"])
        layout = fresh.read_roadmap()
        self.assertEqual(roadmap.Wave(1), layout.placements[WorkItemId("after-checkpoint")])
        self.assertEqual(roadmap.from_state(state), layout)
        view = (fixture.work / "views/items/after-checkpoint.md").read_text()
        self.assertIn("Dependency eligibility: yes", view)
        self.assertIn(f"Checkpoint {checkpoint} accepted", view)
        exported = self.run_json_cli(*fixture.common, "export")
        exported_edges = [self.json_object(value) for value in self.json_array(exported["dependencies"])]
        self.assertTrue(
            any(
                value["item_id"] == "after-checkpoint" and value["checkpoint_id"] == checkpoint
                for value in exported_edges
            )
        )
        self.run_json_cli(*fixture.common, "validate")

        next_brief = replace_struct(
            fixture.brief,
            artifact_revision=2,
            checkpoint=replace_struct(fixture.brief.checkpoint, checkpoint_id="next-phase"),
        )
        published = fixture.call_advertised_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(next_brief))),
            },
        )
        self.assertEqual("committed", published["status"], published)
        reference = self.json_object(published["reference"])
        rebound = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": next_brief.branch,
                "base_revision": next_brief.base_revision,
                "brief_artifact_ref_id": reference["artifact_ref_id"],
            },
        )
        self.assertEqual("committed", rebound["status"], rebound)
        self.assertTrue(self.overview(fixture)["after-checkpoint"]["eligible"])
        old_declaration = self.create(fixture, "late-old-checkpoint", target)
        self.assertEqual("rejected", old_declaration["status"])
        self.assertIn("Known current checkpoint IDs: next-phase", str(old_declaration["message"]))
        next_dependency = self.create(fixture, "after-next", {"item": "work-a", "checkpoint": "next-phase"})
        self.assertEqual("committed", next_dependency["status"], next_dependency)
        self.assertFalse(self.overview(fixture)["after-next"]["eligible"])

        roots: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work), "item_id": "work-a"}
        preview_cancel = fixture.call_advertised_tool(
            mcp_server.CANCEL_TOOL, {"request": {**roots, "operation": "preview"}}
        )
        self.assertEqual(
            "Whole-item dependencies on dropped items remain unsatisfied; matching accepted checkpoints remain satisfied; dependent commitments are retained.",
            self.json_object(preview_cancel["preview"])["dependency_effect"],
        )
        cancelled = fixture.call_advertised_tool(
            mcp_server.CANCEL_TOOL,
            {
                "request": {
                    **roots,
                    "operation": "confirm",
                    "binding": preview_cancel["binding"],
                    "human_confirmation": "Cancel the campaign and retain accepted checkpoint evidence.",
                    "actor_task_id": "coordinator",
                    "actor_host_id": "local",
                }
            },
        )
        self.assertEqual("committed", cancelled["status"], cancelled)
        retained = self.overview(fixture)
        self.assertTrue(retained["after-checkpoint"]["eligible"])
        self.assertFalse(retained["after-campaign"]["eligible"])
        self.assertFalse(retained["after-next"]["eligible"])
        after_cancel = fresh.read_roadmap()
        self.assertEqual(roadmap.Wave(1), after_cancel.placements[WorkItemId("after-checkpoint")])
        self.assertIsInstance(after_cancel.placements[WorkItemId("after-campaign")], roadmap.Blocked)

    def test_closed_checkpoint_child_view_stays_valid_after_rebuild(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        target: JsonObject = {"item": "work-a", "checkpoint": checkpoint}
        self.assertEqual("committed", self.create(fixture, "terminal-child", target)["status"])
        rebuilt, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (output, error))
        action = self.project_action(fixture, "close:terminal-child")
        closed = fixture.call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "receipt": {"action_id": action["action_id"], "subject_revision": action["subject_revision"]},
                "payload": {
                    "outcome": "done",
                    "reason": "Covered by another delivery.",
                    "human_decision": "Close terminal-child as done; its work is covered elsewhere.",
                },
                "actor_task_id": "coordinator",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("committed", closed["status"], closed)
        view_path = fixture.work / "views/items/terminal-child.md"
        refreshed = view_path.read_bytes()
        self.assertIn(f"work-a checkpoint {checkpoint}", refreshed.decode())
        self.assertEqual([], self.run_json_cli(*fixture.common, "validate")["diagnostics"])
        rebuilt, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (output, error))
        self.assertEqual(refreshed, view_path.read_bytes())
        self.assertEqual([], self.run_json_cli(*fixture.common, "validate")["diagnostics"])

    def test_revision_authors_exact_condition_and_rechecks_pinned_brief_under_lock(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        target: JsonObject = {"item": "work-a", "checkpoint": fixture.brief.checkpoint.checkpoint_id}
        self.assertEqual("committed", self.create(fixture, "revised-child", "work-a")["status"])
        current = fixture.store.read_item_definition(WorkItemId("revised-child")).definition
        assert current is not None and isinstance(current.definition, work_models.WorkItemDefinition)
        payload: JsonObject = {
            "schema": "pinboard-item-revision/v1",
            "expected_revision": current.revision,
            "expected_digest": current.digest,
            "source_task": "coordinator",
            "reason": "Start after the accepted checkpoint.",
            "definition": {
                **self.json_object(
                    msgspec.json.decode(expect_success(history.work_item_definition_bytes(current.definition)))
                ),
                "dependencies": [target],
            },
        }
        revised = self.transition_result(fixture, self.project_action(fixture, "revise-item:revised-child"), payload)
        self.assertEqual("committed", revised["status"], revised)
        self.assertTrue(self.overview(fixture)["revised-child"]["eligible"])

        dependency = work_models.CheckpointDependency(
            WorkItemId("work-a"), CheckpointId(fixture.brief.checkpoint.checkpoint_id)
        )
        now = datetime.now(UTC)
        pins = expect_success(
            resolve_dependency_brief_pins(
                fixture.store, ArtifactRepository(resolve_durable_roots(fixture.project)), (dependency,), now
            )
        )
        producer = fixture.store.read_item_definition(dependency.item_id).definition
        assert producer is not None and isinstance(producer.definition, work_models.WorkItemDefinition)
        changed = self.transition_result(
            fixture,
            self.project_action(fixture, "revise-item:work-a"),
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": producer.revision,
                "expected_digest": producer.digest,
                "source_task": "coordinator",
                "reason": "Clarify the producer objective.",
                "definition": {
                    **self.json_object(
                        msgspec.json.decode(expect_success(history.work_item_definition_bytes(producer.definition)))
                    ),
                    "objective": "A revised current objective.",
                },
            },
        )
        self.assertEqual("committed", changed["status"], changed)
        value = proposal()
        value.update(proposal_id="stale-child", relation={"kind": "follow-up", "item": target})
        operation = proposals.convert_proposal(msgspec.convert(value, type=proposal_models.Proposal))
        before = fixture.store.validated_snapshot()
        result = service.create_proposal(
            fixture.store,
            operation,
            now,
            actor_task_id=TaskId("coordinator"),
            actor_host_id=HostId("local"),
            dependency_brief_pins=pins,
        )
        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual("ITEM_DEFINITION_STALE", result.code.value)
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_native_preparation_uses_checkpoint_receipt_and_retains_exclusive_authority(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        target: JsonObject = {"item": "work-a", "checkpoint": fixture.brief.checkpoint.checkpoint_id}
        self.assertEqual("committed", self.create(fixture, "checkpoint-child", target)["status"])
        self.assertEqual("committed", self.create(fixture, "campaign-child", "work-a")["status"])
        request: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "operation": "start",
            "task_id": "preparer",
            "host_id": "local",
            "ttl_seconds": 600,
        }
        before = fixture.store.validated_snapshot()
        blocked = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL, {"request": {**request, "item_id": "campaign-child"}}
        )
        self.assertEqual(("rejected", "ACTION_NOT_AVAILABLE"), (blocked["status"], blocked["code"]))
        self.assertEqual(before, fixture.store.validated_snapshot())
        prepared = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL, {"request": {**request, "item_id": "checkpoint-child"}}
        )
        self.assertEqual("committed", prepared["status"], prepared)
        self.assertEqual(1, len(fixture.store.validated_snapshot().authority.preparation_leases))
        before = fixture.store.validated_snapshot()
        conflict = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {"request": {**request, "item_id": "checkpoint-child", "task_id": "other-preparer"}},
        )
        self.assertEqual("rejected", conflict["status"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_receipt_lookup_is_batched_indexed_and_independent_of_retained_attempts(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        target = work_models.CheckpointDependency(
            WorkItemId("work-a"), CheckpointId(fixture.brief.checkpoint.checkpoint_id)
        )
        with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            expected = sqlite_dependencies.read_accepted_checkpoints(connection, (target,))
            original = connection.execute(
                "SELECT * FROM transition_history WHERE outcome_schema = 'checkpoint-acceptance/v2'"
            ).fetchone()
            assert original is not None
            maximum = connection.execute(
                "SELECT MAX(history_id), MAX(project_revision) FROM transition_history"
            ).fetchone()
            assert maximum is not None
            # Distinct retained attempts and receipt keys must not become eligibility's read scope.
            connection.executemany(
                """INSERT INTO attempts(attempt_id, item_id, state, branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                    candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                    subject_revision, recorded_at, updated_at)
                    SELECT ?, item_id, 'done', branch, base_revision, provenance,
                    brief_artifact_ref_id, brief_artifact_kind, result_artifact_ref_id, result_artifact_kind,
                    candidate_revision, candidate_recorded_at, accepted_scope_revision, accepted_scope_digest,
                    subject_revision, recorded_at, updated_at FROM attempts WHERE attempt_id = ?""",
                tuple((f"retained-{index}", original["subject_id"]) for index in range(1, 1001)),
            )
            connection.executemany(
                """INSERT INTO transition_history(history_id, project_revision, action_id, action_kind, subject_id,
                    artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                    input_schema, input_json, outcome_schema, outcome_json, committed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    (
                        maximum[0] + index,
                        maximum[1] + index,
                        original["action_id"],
                        original["action_kind"],
                        f"retained-{index}",
                        original["artifact_ref_id"],
                        original["artifact_kind"],
                        original["authorization_kind"],
                        original["actor_task_id"],
                        original["actor_host_id"],
                        original["input_schema"],
                        original["input_json"],
                        original["outcome_schema"],
                        original["outcome_json"],
                        original["committed_at"],
                    )
                    for index in range(1, 1001)
                ),
            )
            connection.executemany(
                "INSERT INTO checkpoint_acceptances(history_id, item_id, checkpoint_id) VALUES (?, 'work-a', ?)",
                tuple((maximum[0] + index, target.checkpoint_id) for index in range(1, 1001)),
            )
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            requested = (
                target,
                *(
                    work_models.CheckpointDependency(target.item_id, CheckpointId(f"absent-{index}"))
                    for index in range(400)
                ),
            )
            with patch.object(sqlite_dependencies, "decode_row", wraps=sqlite_dependencies.decode_row) as decoded:
                observed = sqlite_dependencies.read_accepted_checkpoints(connection, requested)
                self.assertEqual(1, decoded.call_count)
                decoded.reset_mock()
                absent = sqlite_dependencies.read_accepted_checkpoints(connection, requested[1:])
                self.assertFalse(absent)
                self.assertEqual(0, decoded.call_count)
            connection.set_trace_callback(None)
            self.assertEqual(expected, observed)
            self.assertLessEqual(len(statements), 5)
            for statement in statements:
                details = tuple(str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement))
                self.assertTrue(
                    any(
                        "checkpoint_acceptances_by_dependency" in row and "item_id=? AND checkpoint_id=?" in row
                        for row in details
                    ),
                    details,
                )
                self.assertTrue(any("SEARCH receipt USING INTEGER PRIMARY KEY" in row for row in details), details)
                self.assertFalse(
                    any("SCAN attempt" in row or "SCAN receipt" in row or "SCAN fact" in row for row in details),
                    details,
                )

    def test_full_validation_detects_missing_and_mismatched_receipt_projection(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        state = fixture.store.validated_snapshot()
        fact = state.lifecycle.checkpoint_acceptances[0]
        with closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection:
            for corruption in ("missing", "mismatched"):
                with self.subTest(corruption=corruption):
                    try:
                        if corruption == "missing":
                            connection.execute(
                                "DELETE FROM checkpoint_acceptances WHERE history_id = ?", (fact.history_id,)
                            )
                        else:
                            connection.execute(
                                "UPDATE checkpoint_acceptances SET checkpoint_id = 'wrong-checkpoint' WHERE history_id = ?",
                                (fact.history_id,),
                            )
                        connection.commit()
                        with self.assertRaises(StorageError):
                            fixture.store.validated_snapshot()
                        if corruption == "mismatched":
                            connection.row_factory = sqlite3.Row
                            with self.assertRaises(StorageError):
                                sqlite_dependencies.read_accepted_checkpoints(
                                    connection,
                                    (work_models.CheckpointDependency(fact.item_id, CheckpointId("wrong-checkpoint")),),
                                )
                    finally:
                        connection.execute(
                            "DELETE FROM checkpoint_acceptances WHERE history_id = ?", (fact.history_id,)
                        )
                        connection.execute(
                            "INSERT INTO checkpoint_acceptances(history_id, item_id, checkpoint_id) VALUES (?, ?, ?)",
                            (fact.history_id, fact.item_id, fact.checkpoint_id),
                        )
                        connection.commit()
                    self.assertEqual(state, fixture.store.validated_snapshot())
