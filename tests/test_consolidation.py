"""Native campaign consolidation commits exactly its separately confirmed selected map."""

import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from unittest.mock import patch

import msgspec

from pinboard.adapters.files.models import UnavailableViewRefresh, ViewWarning
from pinboard.adapters.sqlite import lifecycle
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import consolidation, consolidation_models, stored_state
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, HostId, TaskId, WorkItemId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import execution
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.domain_support import expect_success
from tests.native_support import NativeTools
from tests.support import JsonObject, JsonValue
from tests.test_proposals import proposal


class ConsolidationTest(CheckpointPackageSupport):
    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def current(self, fixture: CheckpointFixture, item: str) -> JsonObject:
        return fixture.call_advertised_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": item}},
        )

    def create(self, fixture: CheckpointFixture, item: str) -> None:
        value = proposal()
        value["proposal_id"] = item
        result = fixture.call_advertised_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {**self.roots(fixture), "proposal": value, "actor_task_id": "human", "actor_host_id": "local"},
        )
        self.assertEqual("committed", result["status"], result)

    def revise(self, fixture: CheckpointFixture, item: str, changes: JsonObject) -> JsonObject:
        current = self.current(fixture, item)
        definition = {**self.json_object(current["definition"]), **changes}
        return fixture.call_advertised_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(
                fixture,
                self.project_action(fixture, f"revise-item:{item}"),
                {
                    "schema": "pinboard-item-revision/v1",
                    "expected_revision": current["definition_revision"],
                    "expected_digest": current["definition_digest"],
                    "source_task": "human",
                    "reason": "Keep the accepted work current.",
                    "definition": definition,
                },
            ),
        )

    def selection(
        self, fixture: CheckpointFixture, sources: tuple[str, ...], campaign: str, create: bool
    ) -> JsonObject:
        definition = self.json_object(self.current(fixture, sources[0] if create else campaign)["definition"])
        definition = {
            **definition,
            "title": "Combined campaign",
            "dependencies": [],
            "checkpoints": [*self.json_array(definition.get("checkpoints", [])), "usable"],
        }
        return {
            "sources": [
                {"item_id": item, "replacement_cost": "The campaign owns this source's complete work."}
                for item in sources
            ],
            "campaign": {"item_id": campaign, "create": create, "definition": definition},
            "checkpoint_targets": [],
        }

    def preview(self, fixture: CheckpointFixture, selection: JsonObject) -> JsonObject:
        return fixture.call_advertised_tool(
            mcp_server.CONSOLIDATE_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "preview",
                    "selection": selection,
                    "actor_task_id": "human",
                }
            },
        )

    def confirm_request(self, fixture: CheckpointFixture, selection: JsonObject, preview: JsonObject) -> JsonObject:
        return {
            "request": {
                **self.roots(fixture),
                "operation": "confirm",
                "selection": selection,
                "binding": preview["binding"],
                "human_confirmation": "Yes, consolidate this complete displayed map.",
                "actor_task_id": "human",
                "actor_host_id": "local",
            }
        }

    def confirm(self, fixture: CheckpointFixture, selection: JsonObject, preview: JsonObject) -> JsonObject:
        return fixture.call_advertised_tool(
            mcp_server.CONSOLIDATE_TOOL, self.confirm_request(fixture, selection, preview)
        )

    def test_new_campaign_carries_distinct_origins_and_converging_waits_through_reload(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        for item in ("source-a", "source-b", "consumer"):
            self.create(fixture, item)
        self.assertEqual(
            "committed", self.revise(fixture, "consumer", {"dependencies": ["source-a", "source-b"]})["status"]
        )
        self.assertEqual(
            "committed",
            self.revise(
                fixture,
                "source-b",
                {
                    "obligations": [
                        {
                            "obligation_id": "sqlite-persistence",
                            "statement": "The proposal persists through SQLite.",
                            "deferral_policy": "allowed",
                        }
                    ]
                },
            )["status"],
        )
        selection = self.selection(fixture, ("source-a", "source-b"), "campaign", True)
        before = fixture.store.validated_snapshot()
        files_before = {
            path.relative_to(fixture.work): path.read_bytes()
            for path in fixture.work.rglob("*")
            if path.is_file() and path.suffix != ".sqlite3"
        }
        preview = self.preview(fixture, selection)
        self.assertEqual("ok", preview["status"], preview)
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual(
            files_before,
            {
                path.relative_to(fixture.work): path.read_bytes()
                for path in fixture.work.rglob("*")
                if path.is_file() and path.suffix != ".sqlite3"
            },
        )
        result = self.confirm(fixture, selection, preview)
        self.assertEqual("committed", result["status"], result)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        after = fresh.validated_snapshot()
        record = msgspec.json.decode(
            bytes(after.transition_receipts[-1].input_payload), type=consolidation_models.Record
        )
        self.assertEqual(
            msgspec.json.encode(preview["preview"], order="sorted"), msgspec.json.encode(record.preview, order="sorted")
        )
        self.assertIsNone(consolidation.receipt_failure(after.transition_receipts[-1]))
        source_definitions: dict[WorkItemId, stored_state.ItemDefinitionRevision] = {
            value.item_id: value
            for value in after.lifecycle.definition_revisions
            if value.item_id in {"source-a", "source-b"}
        }
        campaign = self.json_object(self.current(fixture, "campaign")["definition"])
        carried = [
            self.json_object(value)
            for value in self.json_array(campaign["obligations"])
            if self.json_object(value).get("origin") is not None
        ]
        self.assertEqual(2, len(carried))
        self.assertEqual({"source-a", "source-b"}, {self.json_object(value["origin"])["item_id"] for value in carried})
        for value in carried:
            origin = self.json_object(value["origin"])
            source = source_definitions[WorkItemId(str(origin["item_id"]))]
            self.assertIsInstance(source.definition, work_models.WorkItemDefinition)
            assert isinstance(source.definition, work_models.WorkItemDefinition)
            self.assertEqual(source.revision, origin["definition_revision"])
            self.assertEqual(source.definition.obligations[0].statement, value["statement"])
            self.assertEqual(source.definition.obligations[0].deferral_policy.value, value["deferral_policy"])
            coverage = fixture.call_advertised_tool(
                mcp_server.OBLIGATION_COVERAGE_TOOL,
                {
                    "request": {
                        **self.roots(fixture),
                        "operation": "pair",
                        "source_item": origin["item_id"],
                        "replacement_item": "campaign",
                    }
                },
            )
            self.assertTrue(
                all(
                    self.json_object(row)["status"] == "covered"
                    for row in self.json_array(self.json_object(coverage["coverage"])["obligations"])
                )
            )
        self.assertEqual(
            ["campaign"], self.json_object(self.current(fixture, "consumer")["definition"])["dependencies"]
        )
        dependent = fresh.read_item_status(WorkItemId("consumer"))
        assert dependent is not None
        self.assertFalse(dependent.dependencies[0].satisfied)
        statuses = {value.item_id: value.state for value in after.lifecycle.work_items}
        self.assertEqual(stored_state.StoredWorkItemState.SUPERSEDED, statuses[WorkItemId("source-a")])
        self.assertEqual(stored_state.StoredWorkItemState.SUPERSEDED, statuses[WorkItemId("source-b")])
        overview = fixture.call_advertised_tool(mcp_server.OVERVIEW_TOOL, self.roots(fixture))
        self.assertIn("campaign", str(overview))
        views_before = {
            path.relative_to(fixture.work): path.read_bytes()
            for path in (fixture.work / "views").rglob("*")
            if path.is_file()
        }
        self.assertEqual(0, self.run_cli(*fixture.common, "views", "rebuild")[0])
        self.assertEqual(
            views_before,
            {selector: (fixture.work / selector).read_bytes() for selector in views_before},
        )
        self.assertEqual(0, self.run_cli(*fixture.common, "validate")[0])
        exported = self.run_json_cli(*fixture.common, "export")
        self.assertIn("pinboard-consolidation/v1", str(exported))

    def test_existing_owner_folds_internal_edges_keeps_external_wait_and_declared_target(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        for item in ("source-a", "source-b", "owner", "consumer", "external"):
            self.create(fixture, item)
        self.assertEqual("committed", self.revise(fixture, "external", {"checkpoints": ["ready"]})["status"])
        self.assertEqual("committed", self.revise(fixture, "owner", {"checkpoints": ["owner-keep"]})["status"])
        dependency_cases: tuple[tuple[str, list[JsonValue]], ...] = (
            ("source-b", ["source-a", "external"]),
            ("owner", ["source-a"]),
            ("consumer", [{"item": "external", "checkpoint": "ready"}, "source-b", "zz-proposal-a"]),
        )
        for item, dependencies in dependency_cases:
            self.assertEqual("committed", self.revise(fixture, item, {"dependencies": dependencies})["status"])
        selection = self.selection(fixture, ("source-a", "source-b"), "owner", False)
        selection["checkpoint_targets"] = [
            {
                "consumer": "consumer",
                "checkpoint": "usable",
                "covers_carried_work": "This checkpoint delivers all work previously required from both sources.",
            }
        ]
        preview = self.preview(fixture, selection)
        self.assertEqual("ok", preview["status"], preview)
        self.assertEqual("committed", self.confirm(fixture, selection, preview)["status"])
        owner = self.json_object(self.current(fixture, "owner")["definition"])
        self.assertEqual(["external"], owner["dependencies"])
        self.assertEqual(["owner-keep", "usable"], owner["checkpoints"])
        self.assertIn("original prerequisites source-a, external", str(owner["evidence"]))
        self.assertEqual(
            [{"item": "external", "checkpoint": "ready"}, {"item": "owner", "checkpoint": "usable"}, "zz-proposal-a"],
            self.json_object(self.current(fixture, "consumer")["definition"])["dependencies"],
        )
        facts = fixture.store.read_item_status(WorkItemId("consumer"))
        self.assertIsNotNone(facts)
        assert facts is not None
        self.assertFalse(facts.dependencies[0].satisfied)
        self.assertFalse(facts.dependencies[1].satisfied)
        self.assertEqual(0, self.run_cli(*fixture.common, "validate")[0])

    def test_selected_freshness_wrong_board_and_malformed_confirmation_reject_unchanged(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture, "source")
        selection = self.selection(fixture, ("source",), "campaign", True)
        preview = self.preview(fixture, selection)
        self.assertEqual("ok", preview["status"], preview)
        self.assertEqual("committed", self.revise(fixture, "source", {"title": "Changed source"})["status"])
        before = fixture.store.validated_snapshot()
        self.assertEqual("rejected", self.confirm(fixture, selection, preview)["status"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        fresh = self.preview(fixture, selection)
        for patch_value in (
            {"human_confirmation": " "},
            {"unexpected": True},
            {"binding": {**self.json_object(fresh["binding"]), "work_root": "/another-board"}},
        ):
            with self.subTest(patch=patch_value):
                request = self.confirm_request(fixture, selection, fresh)
                request["request"] = {**self.json_object(request["request"]), **patch_value}
                result = fixture.call_advertised_tool(mcp_server.CONSOLIDATE_TOOL, request)
                self.assertEqual("rejected", result["status"], result)
                self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual("committed", self.revise(fixture, "zz-proposal-a", {"title": "Unrelated change"})["status"])
        self.assertEqual("committed", self.confirm(fixture, selection, fresh)["status"])

    def test_late_failure_rolls_back_and_coordinated_confirmations_accept_once(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture, "source")
        selection = msgspec.convert(
            self.selection(fixture, ("source",), "campaign", True), type=consolidation_models.Selection
        )
        now = datetime(2026, 10, 31, tzinfo=UTC)
        preview = expect_success(consolidation.preview(fixture.store, selection, TaskId("human"), now, ()))
        binding = consolidation.binding(preview, fixture.work)
        before = fixture.store.validated_snapshot()
        with (
            patch.object(sqlite_state, "append_history", side_effect=RuntimeError("late write failed")),
            self.assertRaisesRegex(RuntimeError, "late write failed"),
        ):
            consolidation.confirm(
                fixture.store,
                selection,
                binding,
                "Confirm this map.",
                TaskId("human"),
                HostId("local"),
                now,
                fixture.work,
                (),
            )
        self.assertEqual(before, fixture.store.validated_snapshot())
        barrier = Barrier(2)

        def confirm(_index: int) -> bool:
            barrier.wait(timeout=10)
            return not isinstance(
                consolidation.confirm(
                    fixture.store,
                    selection,
                    binding,
                    "Confirm this map.",
                    TaskId("human"),
                    HostId("local"),
                    now,
                    fixture.work,
                    (),
                ),
                DecisionFailure,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(confirm, range(2)))
        self.assertEqual([False, True], sorted(outcomes))
        self.assertEqual(
            len(before.transition_receipts) + 1, len(fixture.store.validated_snapshot().transition_receipts)
        )

    def test_source_lifecycle_conditions_and_preserved_attempt_fencing(self) -> None:
        for stage in ("active", "review", "paused", "blocked"):
            with self.subTest(stage=stage):
                fixture = self.checkpoint_fixture(local=True) if stage == "review" else self.active_fixture()
                self.native_attempt_acquire(fixture, "source-worker")
                if stage in {"paused", "blocked"}:
                    action = "pause" if stage == "paused" else "block"
                    self.assertEqual(
                        "committed",
                        self.transition_result(
                            fixture,
                            self.project_action(fixture, f"{action}:work-a-1"),
                            {"reason": "Move this work into its campaign."},
                        )["status"],
                    )
                selection = self.selection(fixture, ("work-a",), "campaign", True)
                before = fixture.store.validated_snapshot()
                preview = self.preview(fixture, selection)
                if stage in {"active", "review"}:
                    self.assertEqual("rejected", preview["status"], preview)
                    self.assertEqual(before, fixture.store.validated_snapshot())
                    continue
                self.assertEqual("ok", preview["status"], preview)
                self.assertEqual("committed", self.confirm(fixture, selection, preview)["status"])
                after = fixture.store.validated_snapshot()
                previous = next(value for value in before.lifecycle.attempts if value.attempt_id == "work-a-1")
                retained = next(value for value in after.lifecycle.attempts if value.attempt_id == "work-a-1")
                self.assertEqual(
                    (previous.brief_artifact_ref_id, previous.result_artifact_ref_id, previous.candidate_revision),
                    (retained.brief_artifact_ref_id, retained.result_artifact_ref_id, retained.candidate_revision),
                )
                self.assertEqual(work_models.AttemptState.DONE, retained.state)
                authority = fixture.store.read_attempt_authority_status(AttemptId("work-a-1"))
                assert authority is not None
                self.assertEqual("revoked", authority.status.value)
                inspection = fixture.call_advertised_tool(
                    mcp_server.ATTEMPT_INSPECT_TOOL,
                    {**self.roots(fixture), "attempt_id": "work-a-1", "reconciliation": None},
                )
                self.assertEqual("superseded", self.json_object(inspection["continuation"])["state"])

    def test_active_and_review_campaign_or_dependent_revision_preserves_candidate_and_authority(self) -> None:
        for stage in ("active", "review"):
            for target in ("campaign", "dependent"):
                with self.subTest(stage=stage, target=target):
                    fixture = self.checkpoint_fixture(local=True) if stage == "review" else self.active_fixture()
                    self.create(fixture, "source")
                    self.native_attempt_acquire(fixture, "target-worker")
                    if target == "dependent":
                        self.assertEqual(
                            "committed", self.revise(fixture, "work-a", {"dependencies": ["source"]})["status"]
                        )
                    selection = self.selection(
                        fixture, ("source",), "work-a" if target == "campaign" else "campaign", target != "campaign"
                    )
                    before = fixture.store.validated_snapshot()
                    authority = fixture.store.read_attempt_authority_status(AttemptId("work-a-1"))
                    preview = self.preview(fixture, selection)
                    self.assertEqual("ok", preview["status"], preview)
                    self.assertEqual("committed", self.confirm(fixture, selection, preview)["status"])
                    after = fixture.store.validated_snapshot()
                    self.assertEqual(
                        next(value for value in before.lifecycle.attempts if value.attempt_id == "work-a-1"),
                        next(value for value in after.lifecycle.attempts if value.attempt_id == "work-a-1"),
                    )
                    self.assertEqual(authority, fixture.store.read_attempt_authority_status(AttemptId("work-a-1")))
                    attempt = next(value for value in after.lifecycle.attempts if value.attempt_id == "work-a-1")
                    current = self.current(fixture, "work-a")
                    self.assertNotEqual(
                        (attempt.accepted_scope_revision, attempt.accepted_scope_digest),
                        (current["definition_revision"], current["definition_digest"]),
                    )

    def test_preparation_invalid_targets_and_cycle_refusals_leave_state_unchanged(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        for item in ("source", "owner", "consumer", "external"):
            self.create(fixture, item)
        self.assertEqual("committed", self.revise(fixture, "consumer", {"dependencies": ["source"]})["status"])
        selection = self.selection(fixture, ("source",), "owner", False)
        campaign = self.json_object(selection["campaign"])
        definition = self.json_object(campaign["definition"])
        cases: tuple[tuple[list[JsonValue], list[JsonValue]], ...] = (
            (["owner"], []),
            (["consumer"], []),
            (["missing"], []),
            ([{"item": "external", "checkpoint": "missing"}], []),
            ([], [{"consumer": "consumer", "checkpoint": "missing", "covers_carried_work": "All carried work."}]),
        )
        for dependencies, targets in cases:
            with self.subTest(dependencies=dependencies, targets=targets):
                invalid: JsonObject = {
                    **selection,
                    "campaign": {**campaign, "definition": {**definition, "dependencies": dependencies}},
                    "checkpoint_targets": targets,
                }
                before = fixture.store.validated_snapshot()
                self.assertEqual("rejected", self.preview(fixture, invalid)["status"])
                self.assertEqual(before, fixture.store.validated_snapshot())
        prepared = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "start",
                    "item_id": "source",
                    "task_id": "preparer",
                    "host_id": "local",
                    "ttl_seconds": 3600,
                }
            },
        )
        self.assertEqual("committed", prepared["status"], prepared)
        before = fixture.store.validated_snapshot()
        self.assertEqual("rejected", self.preview(fixture, selection)["status"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_focused_preview_and_committed_view_warning_repair(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture, "source")
        for index in range(8):
            self.create(fixture, f"unrelated-{index}")
        selection = self.selection(fixture, ("source",), "campaign", True)
        decoded = []
        original = lifecycle.decode_definition_revision

        def decode(row: sqlite3.Row) -> stored_state.ItemDefinitionRevision:
            value = original(row)
            decoded.append(value.item_id)
            return value

        with (
            patch.object(lifecycle, "decode_definition_revision", side_effect=decode),
            patch.object(
                sqlite_state, "_read_history", side_effect=AssertionError("focused preview read retained history")
            ),
        ):
            preview = self.preview(fixture, selection)
        self.assertEqual("ok", preview["status"], preview)
        self.assertFalse(any(str(value).startswith("unrelated-") for value in decoded))
        with patch.object(
            mcp_common,
            "_refresh_affected_views",
            return_value=UnavailableViewRefresh(ViewWarning("Views need repair.", "Run 'pinboard views rebuild'.")),
        ):
            committed = self.confirm(fixture, selection, preview)
        self.assertEqual(
            ("committed-with-warning", "committed", "do-not-retry"),
            (committed["status"], committed["effect"], committed["retry"]),
        )
        self.assertEqual("rejected", self.confirm(fixture, selection, preview)["status"])
        self.assertEqual(0, self.run_cli(*fixture.common, "views", "rebuild")[0])
        self.assertEqual(0, self.run_cli(*fixture.common, "validate")[0])

    def test_live_prose_replacement_repairs_exact_current_origins_and_terminal_stays_read_only(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        for item in ("source", "replacement"):
            self.create(fixture, item)
        self.assertEqual("committed", self.revise(fixture, "source", {"title": "Current source"})["status"])
        relation: JsonObject = {
            "schema": "pinboard-planned-replacement/v1",
            "expected_relation_revision": 0,
            "replacement_item": "replacement",
            "replacement_cost": "Replacement carries this work.",
            "status": "current",
            "recorded_by": "human",
        }
        self.assertEqual(
            "committed",
            self.transition_result(fixture, self.project_action(fixture, "record-replacement:source"), relation)[
                "status"
            ],
        )
        request: JsonObject = {
            "request": {**self.roots(fixture), "operation": "planned-replacement", "item_id": "source"}
        }
        before_coverage = fixture.call_advertised_tool(mcp_server.OBLIGATION_COVERAGE_TOOL, request)
        self.assertIn("uncovered", str(before_coverage["coverage"]))
        source = self.current(fixture, "source")
        definition = self.json_object(self.current(fixture, "replacement")["definition"])
        obligation = self.json_object(self.json_array(definition["obligations"])[0])
        exact: JsonObject = {
            **obligation,
            "origin": {
                "item_id": "source",
                "definition_revision": source["definition_revision"],
                "obligation_id": "sqlite-persistence",
            },
        }
        bad: JsonObject = {
            **exact,
            "origin": {"item_id": "source", "definition_revision": 999, "obligation_id": "sqlite-persistence"},
        }
        before = fixture.store.validated_snapshot()
        self.assertEqual(
            "rejected",
            self.revise(fixture, "replacement", {"schema": "pinboard-work-item-definition/v3", "obligations": [bad]})[
                "status"
            ],
        )
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual(
            "committed",
            self.revise(fixture, "replacement", {"schema": "pinboard-work-item-definition/v3", "obligations": [exact]})[
                "status"
            ],
        )
        covered = fixture.call_advertised_tool(mcp_server.OBLIGATION_COVERAGE_TOOL, request)
        self.assertNotIn("uncovered", str(covered["coverage"]))
        current = self.current(fixture, "replacement")
        stale_action = self.project_action(fixture, "revise-item:replacement")
        close = fixture.call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            {
                **self.roots(fixture),
                "receipt": {
                    "action_id": self.project_action(fixture, "close:replacement")["action_id"],
                    "subject_revision": self.project_action(fixture, "close:replacement")["subject_revision"],
                },
                "payload": {
                    "outcome": "done",
                    "reason": "Replacement delivered.",
                    "human_decision": "Close the completed replacement.",
                },
                "actor_task_id": "human",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("committed", close["status"], close)
        after = fixture.store.validated_snapshot()
        rejected = self.transition_result(
            fixture,
            stale_action,
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current["definition_revision"],
                "expected_digest": current["definition_digest"],
                "source_task": "human",
                "reason": "Attempt terminal revision.",
                "definition": current["definition"],
            },
        )
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(after, fixture.store.validated_snapshot())
        self.assertEqual(
            covered["coverage"], fixture.call_advertised_tool(mcp_server.OBLIGATION_COVERAGE_TOOL, request)["coverage"]
        )

    def test_changed_dependent_relation_and_authority_invalidate_the_selected_map(self) -> None:
        for changed in ("dependent", "relation", "authority"):
            with self.subTest(changed=changed):
                fixture = self.checkpoint_fixture(local=True)
                for item in ("source", "consumer", "other-owner"):
                    self.create(fixture, item)
                self.assertEqual("committed", self.revise(fixture, "consumer", {"dependencies": ["source"]})["status"])
                selection = self.selection(fixture, ("source",), "work-a", False)
                preview = self.preview(fixture, selection)
                self.assertEqual("ok", preview["status"], preview)
                if changed == "dependent":
                    self.assertEqual(
                        "committed", self.revise(fixture, "consumer", {"title": "Changed consumer"})["status"]
                    )
                elif changed == "relation":
                    relation: JsonObject = {
                        "schema": "pinboard-planned-replacement/v1",
                        "expected_relation_revision": 0,
                        "replacement_item": "other-owner",
                        "replacement_cost": "Another replacement was selected.",
                        "status": "current",
                        "recorded_by": "human",
                    }
                    self.assertEqual(
                        "committed",
                        self.transition_result(
                            fixture, self.project_action(fixture, "record-replacement:source"), relation
                        )["status"],
                    )
                else:
                    self.native_attempt_acquire(fixture, "new-target-worker")
                before = fixture.store.validated_snapshot()
                self.assertEqual("rejected", self.confirm(fixture, selection, preview)["status"])
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_automatic_preflight_refuses_without_running_confirmation(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        self.create(fixture, "source")
        selection = self.selection(fixture, ("source",), "campaign", True)
        preview = self.preview(fixture, selection)
        before = fixture.store.validated_snapshot()
        settings = fixture.work / "contributor-traces.config"
        original_settings = settings.read_bytes() if settings.exists() else None
        settings.write_text("[trace]\nmode = invalid\n")
        executor = execution.BoundedExecutor(worker_count=1, unfinished_limit=1)
        try:
            native = NativeTools(
                mcp_server.create_server(
                    executor,
                    execution.Diagnostics(io.StringIO(), event_limit=4, line_limit=256),
                    execution.AutomaticCapture(mcp_common.select_capture_item),
                )
            )
            refused = native.call_advertised_tool(
                mcp_server.CONSOLIDATE_TOOL, self.confirm_request(fixture, selection, preview)
            )
        finally:
            executor.shutdown()
            if original_settings is None:
                settings.unlink()
            else:
                settings.write_bytes(original_settings)
        self.assertEqual("TRACE_PREFLIGHT_FAILED", refused["code"], refused)
        self.assertIs(False, refused["target_ran"])
        self.assertEqual(before, fixture.store.validated_snapshot())
