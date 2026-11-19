import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.sqlite import dependencies as sqlite_dependencies
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    roadmap,
    stored_state,
    work_brief_models,
)
from pinboard.domain import history, work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import CheckpointId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport, predecessor_checkpoint_fixture
from tests.domain_support import expect_success
from tests.support import JsonObject, JsonValue
from tests.test_proposals import proposal
from tests.test_work_item_definitions import definition
from tests.work_brief_support import work_c_brief


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
        related_item = WorkItemId(str(target["item"] if isinstance(target, dict) else target))
        state = fixture.store.validated_snapshot()
        value["related_disposition"] = {
            "kind": "related",
            "item_id": related_item,
            "active_memberships": [
                initiative.initiative_id
                for initiative in state.initiatives
                for member in initiative.memberships
                if member.item_id == related_item and member.removed is None
            ],
            "active_off_roadmap": any(
                episode.item_id == related_item and episode.closed is None for episode in state.off_roadmap_episodes
            ),
        }
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
                "max_items": 25,
                "continuation": None,
            },
        )
        self.assertIsNone(value["continuation"])
        return {str(row["item_id"]): row for raw in self.json_array(value["items"]) if (row := self.json_object(raw))}

    def test_saved_checkpoint_acceptance_fresh_reload_views_export_and_cancellation_agree(self) -> None:  # noqa: PLR0915 - one persisted checkpoint dependency journey
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        target: JsonObject = {"item": "work-a", "checkpoint": checkpoint}
        before = fixture.store.validated_snapshot()
        invalid = self.create(fixture, "new-checkpoint", target)
        self.assertEqual(("rejected", "ITEM_DEFINITION_INVALID"), (invalid["status"], invalid["code"]))
        self.assertIn(f"work-a checkpoint {checkpoint}", str(invalid["message"]))
        self.assertEqual(
            (False, "unchanged", []), (invalid["state_changed"], invalid["effect"], invalid["changed_surfaces"])
        )
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        with predecessor_checkpoint_fixture():
            created = self.create(fixture, "after-checkpoint", target)
            self.assertEqual("committed", created["status"], created)
        created = self.create(fixture, "after-campaign", "work-a")
        self.assertEqual("committed", created["status"], created)
        initial = self.overview(fixture)
        self.assertEqual(1, initial["after-checkpoint"]["unsatisfied_dependency_count"])
        self.assertEqual(1, initial["after-campaign"]["unsatisfied_dependency_count"])

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
        self.assertEqual(0, current["after-checkpoint"]["unsatisfied_dependency_count"])
        self.assertEqual(1, current["after-campaign"]["unsatisfied_dependency_count"])
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
        self.assertTrue(self.json_object(self.json_array(status["dependency_reasons"])[0])["checkpoint_accepted"])
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

        self.rebind_to_next_phase(fixture)
        self.assertEqual(0, self.overview(fixture)["after-checkpoint"]["unsatisfied_dependency_count"])
        with predecessor_checkpoint_fixture():
            future = self.create(fixture, "after-next", {"item": "work-a", "checkpoint": "next-phase"})
            self.assertEqual("committed", future["status"], future)

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
        self.assertEqual(0, retained["after-checkpoint"]["unsatisfied_dependency_count"])
        self.assertEqual(1, retained["after-campaign"]["unsatisfied_dependency_count"])
        self.assertEqual(1, retained["after-next"]["unsatisfied_dependency_count"])
        after_cancel = fresh.read_roadmap()
        self.assertEqual(roadmap.Wave(1), after_cancel.placements[WorkItemId("after-checkpoint")])
        self.assertIsInstance(after_cancel.placements[WorkItemId("after-campaign")], roadmap.Blocked)
        before = fresh.validated_snapshot()
        invalid = self.create(fixture, "after-cancel-accepted", target)
        self.assertEqual(("rejected", "ITEM_DEFINITION_INVALID"), (invalid["status"], invalid["code"]))
        self.assertEqual(before, fresh.validated_snapshot())

    def rebind_to_next_phase(self, fixture: CheckpointFixture) -> None:
        """Move work-a's current accepted brief to a later checkpoint so the first one is no longer declared."""

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

    def revise(self, fixture: CheckpointFixture, item_id: str, reason: str, **changes: JsonValue) -> JsonObject:
        current = SQLiteWorkStore(fixture.work / "state.sqlite3").read_item_definition(WorkItemId(item_id)).definition
        assert current is not None and isinstance(current.definition, work_models.WorkItemDefinition)
        return fixture.call_advertised_tool(
            mcp_server.TRANSITION_TOOL,
            self.native_transition_request(
                fixture,
                self.project_action(fixture, f"revise-item:{item_id}"),
                {
                    "schema": "pinboard-item-revision/v1",
                    "expected_revision": current.revision,
                    "expected_digest": current.digest,
                    "source_task": "coordinator",
                    "reason": reason,
                    "definition": {
                        **self.json_object(
                            msgspec.json.decode(expect_success(history.work_item_definition_bytes(current.definition)))
                        ),
                        **changes,
                    },
                },
            ),
        )

    def reloaded_definition(self, fixture: CheckpointFixture, item_id: str) -> work_models.WorkItemDefinition:
        current = SQLiteWorkStore(fixture.work / "state.sqlite3").read_item_definition(WorkItemId(item_id)).definition
        assert current is not None and isinstance(current.definition, work_models.WorkItemDefinition)
        return current.definition

    def complete_work_a(self, fixture: CheckpointFixture) -> None:
        fixture = self.terminalize_brief(fixture)
        self.record_commissioned_review(fixture, fixture.candidate_revision, "independent-reviewer")
        attempt_root = fixture.work / "attempts" / "work-a-1"
        completed = self.transition_result(
            fixture,
            self.project_action(fixture, "complete:work-a-1"),
            {
                "schema": "pinboard-reviewed-completion/v3",
                "human_decision": "Complete this item with its reviewed outcome.",
                "candidate": fixture.candidate_revision,
                "evidence": "Accepted by the maintainer.",
                "reviewer_task_id": "independent-reviewer",
                "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                "packages": [],
            },
        )
        self.assertEqual("committed", completed["status"], completed)

    def test_unchanged_brief_checkpoint_dependency_survives_producer_completion_and_reloads(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = CheckpointId(fixture.brief.checkpoint.checkpoint_id)
        target: JsonObject = {"item": "work-a", "checkpoint": checkpoint}
        dependency = work_models.CheckpointDependency(WorkItemId("work-a"), checkpoint)
        self.assertNotIn(checkpoint, self.reloaded_definition(fixture, "work-a").checkpoints)
        with predecessor_checkpoint_fixture():
            self.assertEqual("committed", self.create(fixture, "brief-child", target)["status"])

        self.complete_work_a(fixture)
        revised = self.revise(fixture, "brief-child", "Clarify the wording only.", objective="A clarified objective.")
        self.assertEqual("committed", revised["status"], revised)
        reloaded = self.reloaded_definition(fixture, "brief-child")
        self.assertEqual(("A clarified objective.", (dependency,)), (reloaded.objective, reloaded.dependencies))
        self.assertEqual(0, self.overview(fixture)["brief-child"]["unsatisfied_dependency_count"])

        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        changed = self.revise(
            fixture,
            "brief-child",
            "Wait for a different phase of the finished producer.",
            dependencies=[{"item": "work-a", "checkpoint": "later-phase"}],
        )
        self.assertEqual(("rejected", "ITEM_DEFINITION_INVALID"), (changed["status"], changed["code"]))
        self.assertIn("work-a checkpoint later-phase", str(changed["message"]))
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_revision_keeps_a_checkpoint_dependency_no_authoring_route_could_verify_again(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        target: JsonObject = {"item": "work-a", "checkpoint": fixture.brief.checkpoint.checkpoint_id}
        with predecessor_checkpoint_fixture():
            self.assertEqual("committed", self.create(fixture, "kept-child", target)["status"])
        stale_brief = self.revise(
            fixture, "work-a", "Clarify the producer objective.", objective="A revised objective."
        )
        self.assertEqual("committed", stale_brief["status"], stale_brief)
        dependency = work_models.CheckpointDependency(WorkItemId("work-a"), CheckpointId(str(target["checkpoint"])))
        kept = self.revise(fixture, "kept-child", "Reword only.", unlock="A reworded unlock.")
        self.assertEqual("committed", kept["status"], kept)
        reloaded = self.reloaded_definition(fixture, "kept-child")
        self.assertEqual(
            ("A reworded unlock.", (dependency,)),
            (reloaded.unlock, reloaded.dependencies),
        )

    def test_revise_refuses_new_wait_for_a_checkpoint_already_accepted(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        target: JsonObject = {"item": "work-a", "checkpoint": fixture.brief.checkpoint.checkpoint_id}
        self.assertEqual("committed", self.create(fixture, "bare-child", "work-a")["status"])
        self.rebind_to_next_phase(fixture)
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        revised = self.revise(fixture, "bare-child", "Start after the accepted checkpoint.", dependencies=[target])
        self.assertEqual(("rejected", "ITEM_DEFINITION_INVALID"), (revised["status"], revised["code"]))
        self.assertEqual(
            (False, "unchanged", []), (revised["state_changed"], revised["effect"], revised["changed_surfaces"])
        )
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        self.assertEqual((WorkItemId("work-a"),), self.reloaded_definition(fixture, "bare-child").dependencies)

    def test_started_items_refuse_even_satisfied_edges_and_companion_proposals(self) -> None:
        for stage in ("active", "review", "paused", "blocked"):
            with self.subTest(stage=stage):
                if stage in {"active", "blocked"}:
                    fixture = self.active_fixture()
                elif stage == "paused":
                    fixture = self.accepted_package_fixture(local=True)
                else:
                    fixture = self.checkpoint_fixture(local=True)
                if stage == "blocked":
                    result = self.transition_result(
                        fixture,
                        self.project_action(fixture, "block:work-a-1"),
                        {"unblocker": {"kind": "outside-condition", "condition": "The offline test device returns."}},
                    )
                    self.assertEqual("committed", result["status"], result)
                fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
                before = fresh.validated_snapshot()
                self.assertEqual(
                    stage, next(item.state.value for item in before.lifecycle.work_items if item.item_id == "work-a")
                )
                added = self.revise(
                    fixture, "work-a", "Add the completed prerequisite.", dependencies=["work-b"], title="Edited title"
                )
                intake = proposal()
                intake.update(proposal_id="companion", relation={"kind": "prerequisite", "item": "work-a"})
                companion = fixture.call_advertised_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "proposal": intake,
                        "actor_task_id": "coordinator",
                        "actor_host_id": "local",
                    },
                )
                for rejection, edge in ((added, "work-b"), (companion, "companion")):
                    self.assertEqual(
                        ("rejected", "ITEM_DEFINITION_LIFECYCLE_INVALID", False, "unchanged", []),
                        (
                            rejection["status"],
                            rejection["code"],
                            rejection["state_changed"],
                            rejection["effect"],
                            rejection["changed_surfaces"],
                        ),
                    )
                    self.assertIn("Started item 'work-a'", str(rejection["message"]))
                    self.assertIn(edge, str(rejection["message"]))
                self.assertEqual(before, fresh.validated_snapshot())
                clarified = self.revise(fixture, "work-a", "Clarify only.", title="Edited title")
                self.assertEqual("committed", clarified["status"], clarified)
                self.assertEqual("Edited title", self.reloaded_definition(fixture, "work-a").title)

    def test_unstarted_blocked_item_can_add_and_reload_a_whole_item_prerequisite(self) -> None:
        fixture = self.active_fixture()
        blocked = self.transition_result(
            fixture,
            self.project_action(fixture, "block-item:work-c"),
            {"unblocker": {"kind": "outside-condition", "condition": "The offline test device returns."}},
        )
        self.assertEqual("committed", blocked["status"], blocked)
        revised = self.revise(
            fixture, "work-c", "Record the needed work before starting.", dependencies=["intake-work"]
        )
        self.assertEqual("committed", revised["status"], revised)
        self.assertEqual((WorkItemId("intake-work"),), self.reloaded_definition(fixture, "work-c").dependencies)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        status = fresh.read_item_status(WorkItemId("work-c"))
        assert status is not None
        self.assertFalse(status.dependencies[0].satisfied)

    def hold_sentence(self, fixture: CheckpointFixture, item_id: str) -> str:
        status = fixture.call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "item_id": item_id,
                }
            },
        )
        return str(self.json_object(status["hold"])["sentence"])

    def _activate_independent_child(self, fixture: CheckpointFixture, item_id: str) -> None:
        independent = proposal()
        independent.update(proposal_id=item_id)
        created = fixture.call_advertised_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "proposal": independent,
                "actor_task_id": "coordinator",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("committed", created["status"], created)
        prepared = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "start",
                    "item_id": item_id,
                    "task_id": "coordinator",
                    "host_id": "local",
                    "ttl_seconds": 600,
                }
            },
        )
        self.assertEqual("committed", prepared["status"], prepared)
        template = work_c_brief()
        local = template.checkpoint
        assert isinstance(local, work_brief_models.LocalCheckpoint)
        brief = replace_struct(
            template,
            attempt_id=f"{item_id}-1",
            item_id=item_id,
            artifact_revision=1,
            branch=fixture.brief.branch,
            base_revision=fixture.brief.base_revision,
            owner_task_id="coordinator",
            checkout_selection=fixture.brief.checkout_selection,
            accepted_scope=work_brief_models.AcceptedScope(1, str(prepared["definition_digest"])),
            checkpoint=replace_struct(
                local,
                architecture_impact=work_brief_models.NoArchitectureImpact("No architecture impact."),
                disposition=work_brief_models.TerminalCheckpointDisposition(),
                verification=(
                    replace_struct(
                        local.verification[0],
                        authorization_basis=work_brief_models.AcceptedScopeAuthorization(item_id, 1),
                    ),
                ),
            ),
            obligation_correspondence=(
                work_brief_models.ObligationCorrespondence(
                    "sqlite-persistence", work_brief_models.CriterionObligationTarget(1)
                ),
            ),
        )
        published = fixture.call_advertised_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief))),
            },
        )
        self.assertEqual("committed", published["status"], published)
        activate = self.native_actions(fixture, "activate", item_id, role="preparer", lease=prepared)
        activated = fixture.call_native_tool(
            mcp_server.TRANSITION_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "role": "preparer",
                    "receipt": {
                        "action_id": activate["action_id"],
                        "subject_revision": activate["subject_revision"],
                    },
                    "payload": {"brief_artifact_ref_id": self.json_object(published["reference"])["artifact_ref_id"]},
                    "lease_id": activate["lease_id"],
                    "generation": activate["generation"],
                }
            },
        )
        self.assertEqual("committed", activated["status"], activated)

    def test_held_work_says_its_checkpoint_was_accepted_and_then_that_it_no_longer_depends_on_the_item(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        with predecessor_checkpoint_fixture():
            self.assertEqual(
                "committed",
                self.create(fixture, "unstarted-child", {"item": "work-a", "checkpoint": checkpoint})["status"],
            )
        unstarted_held = self.transition_result(
            fixture,
            self.project_action(fixture, "block-item:unstarted-child"),
            {"unblocker": {"kind": "items", "items": ["work-a"]}},
        )
        self.assertEqual("committed", unstarted_held["status"], unstarted_held)
        self._activate_independent_child(fixture, "held-child")
        initial = next(
            value
            for value in reversed(fixture.store.validated_snapshot().lifecycle.definition_revisions)
            if value.item_id == WorkItemId("held-child")
        )
        initial_bytes = stored_state.stored_definition_bytes(initial.definition)
        assert isinstance(initial_bytes, bytes)
        with_dependency = self.json_object(json.loads(initial_bytes))
        with_dependency["dependencies"] = [{"item": "work-a", "checkpoint": checkpoint}]
        with predecessor_checkpoint_fixture():
            added_dependency = self.transition_result(
                fixture,
                self.project_action(fixture, "revise-item:held-child"),
                {
                    "schema": "pinboard-item-revision/v1",
                    "expected_revision": initial.revision,
                    "expected_digest": initial.digest,
                    "source_task": "coordinator",
                    "reason": "Depend on the local checkpoint of work-a.",
                    "definition": with_dependency,
                },
            )
        self.assertEqual("committed", added_dependency["status"], added_dependency)
        held = self.transition_result(
            fixture,
            self.project_action(fixture, "block:held-child-1"),
            {"unblocker": {"kind": "items", "items": ["work-a"]}},
        )
        self.assertEqual("committed", held["status"], held)
        self.assertIn(f"checkpoint {checkpoint} of work-a to be accepted", self.hold_sentence(fixture, "held-child"))

        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        accepted = self.transition_result(fixture, action, self.json_object(json.loads(fixture.payload.read_bytes())))
        self.assertEqual("committed", accepted["status"], accepted)
        unstarted_status = fixture.call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {
                "request": {
                    "operation": "item",
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "item_id": "unstarted-child",
                }
            },
        )
        self.assertEqual(("ready", None), (unstarted_status["state"], unstarted_status["hold"]))
        sentence = self.hold_sentence(fixture, "held-child")
        item_view = (fixture.work / "views/items/held-child.md").read_text()
        roadmap_view = (fixture.work / "views/roadmap.md").read_text()
        for text in (sentence, item_view, roadmap_view):
            self.assertIn(f"Checkpoint {checkpoint} of ", text)
            self.assertIn("was accepted", text)
            self.assertIn("can continue once someone resumes it", text)
            self.assertNotIn("has finished", text)
        self.assertEqual(
            "ok",
            self.actions_result(fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "held-child"}})[
                "status"
            ],
        )
        views = sorted(path for path in (fixture.work / "views").rglob("*") if path.is_file())
        before = {path: path.read_bytes() for path in views}
        rebuilt, output, error = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (output, error))
        self.assertEqual(before, {path: path.read_bytes() for path in views})

        current = next(
            value
            for value in reversed(fixture.store.validated_snapshot().lifecycle.definition_revisions)
            if value.item_id == WorkItemId("held-child")
        )
        definition_bytes = stored_state.stored_definition_bytes(current.definition)
        assert isinstance(definition_bytes, bytes)
        revised_definition = self.json_object(json.loads(definition_bytes))
        no_dependencies: list[JsonValue] = []
        revised_definition["dependencies"] = no_dependencies
        revised = self.transition_result(
            fixture,
            self.project_action(fixture, "revise-item:held-child"),
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current.revision,
                "expected_digest": current.digest,
                "source_task": "coordinator",
                "reason": "The held work no longer depends on work-a.",
                "definition": revised_definition,
            },
        )
        self.assertEqual("committed", revised["status"], revised)
        sentence = self.hold_sentence(fixture, "held-child")
        self.assertIn("work-a is no longer one of this item's dependencies", sentence)
        self.assertNotIn("was accepted", sentence)
        self.assertNotIn("has finished", sentence)

    def test_closed_checkpoint_child_view_stays_valid_after_rebuild(self) -> None:
        fixture = self.checkpoint_fixture(local=True)
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        target: JsonObject = {"item": "work-a", "checkpoint": checkpoint}
        with predecessor_checkpoint_fixture():
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

    def test_native_preparation_uses_checkpoint_receipt_and_retains_exclusive_authority(self) -> None:
        fixture = self.accepted_package_fixture(local=True)
        target: JsonObject = {"item": "work-a", "checkpoint": fixture.brief.checkpoint.checkpoint_id}
        with predecessor_checkpoint_fixture():
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
