"""Automatic release of unstarted items holds, Ready to resume views, overview v10, and validation invariants."""

import contextlib
import dataclasses
import hashlib
import json
import re
import sqlite3
import subprocess
import unittest
from datetime import UTC, datetime

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.adapters.files import views as views_module
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.lifecycle import move_item_state_count
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    consolidation_models,
    pr_reviews,
    project_export,
    stored_state,
    work_brief_models,
)
from pinboard.domain import decision_models, history, routing, work_models
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    AttemptId,
    CheckpointId,
    LeaseId,
    TaskId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointFixture, CheckpointPackageSupport
from tests.domain_support import action, attempt_record, expect_success
from tests.native_support import native_tools
from tests.sqlite_support import sync_test_unblocker
from tests.support import SQLITE_DIGEST as DIGEST
from tests.support import JsonObject, JsonValue
from tests.test_proposals import proposal
from tests.test_work_item_definitions import definition as sample_definition
from tests.work_brief_support import work_c_brief

NOW = datetime(2026, 11, 18, tzinfo=UTC)


def work_item(
    item_id: str,
    state: work_models.WorkState,
    *,
    attempt: str | None = None,
    depends_on: tuple[work_models.Dependency, ...] = (),
    unblocker: work_models.Unblocker | None = None,
) -> work_models.WorkItem:
    return work_models.WorkItem(
        WorkItemId(item_id),
        state,
        None,
        depends_on,
        None if attempt is None else AttemptId(attempt),
        None,
        None,
        None,
        1,
        unblocker=unblocker,
    )


def _anchor(
    item_id: str,
    dependencies: tuple[work_models.Dependency, ...] = (),
    *,
    classified: bool = True,
) -> work_models.DefinitionAnchor:
    base = sample_definition()
    route_facts = base.route_facts if classified else None
    updated = dataclasses.replace(base, dependencies=dependencies, route_facts=route_facts)
    digest = history.work_item_definition_digest(updated)
    assert isinstance(digest, str)
    return work_models.DefinitionAnchor(WorkItemId(item_id), 1, digest, updated)


class AutomaticHoldReleaseDomainTest(unittest.TestCase):
    def test_releasable_unstarted_hold_predicate_across_states_attempts_unblockers_dependencies_and_replacements(
        self,
    ) -> None:
        checkpoint_dep = work_models.CheckpointDependency(WorkItemId("campaign"), CheckpointId("foundation"))
        items_hold = work_models.WaitingForItemsToFinish((WorkItemId("prereq"),))
        checkpoint_hold = work_models.WaitingForItemsToFinish((WorkItemId("campaign"),))
        human_hold = work_models.WaitingForHumanDecision("Approve rollout?")
        outside_hold = work_models.WaitingForOutsideCondition("Upstream tag exists.")

        unstarted_satisfied = work_item(
            "dep-a",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"),),
            unblocker=items_hold,
        )
        paused_unstarted = work_item(
            "dep-paused",
            work_models.WorkState.PAUSED,
            depends_on=(checkpoint_dep,),
            unblocker=checkpoint_hold,
        )
        started_satisfied = work_item(
            "dep-started",
            work_models.WorkState.BLOCKED,
            attempt="dep-started-1",
            depends_on=(WorkItemId("prereq"),),
            unblocker=items_hold,
        )
        ready_item = work_item("dep-ready", work_models.WorkState.READY, depends_on=(WorkItemId("prereq"),))
        human_blocked = work_item(
            "dep-human",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"),),
            unblocker=human_hold,
        )
        outside_blocked = work_item(
            "dep-outside",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"),),
            unblocker=outside_hold,
        )
        still_waiting = work_item(
            "dep-waiting",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"), WorkItemId("live-other")),
            unblocker=items_hold,
        )
        cancelled_waiting = work_item(
            "dep-cancelled",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("dropped-prereq"),),
            unblocker=work_models.WaitingForItemsToFinish((WorkItemId("dropped-prereq"),)),
        )
        replacement_withheld = work_item(
            "dep-replaced",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"),),
            unblocker=items_hold,
        )
        replacement_resolved = work_item(
            "dep-retained",
            work_models.WorkState.BLOCKED,
            depends_on=(WorkItemId("prereq"),),
            unblocker=items_hold,
        )

        snapshot = LedgerSnapshot(
            "rev-1",
            (
                work_item("live-other", work_models.WorkState.READY),
                work_item("campaign", work_models.WorkState.ACTIVE, attempt="campaign-1"),
                unstarted_satisfied,
                paused_unstarted,
                started_satisfied,
                ready_item,
                human_blocked,
                outside_blocked,
                still_waiting,
                cancelled_waiting,
                replacement_withheld,
                replacement_resolved,
            ),
            accepted_checkpoints=frozenset((checkpoint_dep,)),
            dependency_facts=(),
            dropped_item_ids=frozenset((WorkItemId("dropped-prereq"),)),
            planned_replacements=(
                work_models.PlannedReplacement(
                    WorkItemId("dep-replaced"),
                    1,
                    WorkItemId("replacer"),
                    "Replaced.",
                    work_models.PlannedReplacementStatus.CURRENT,
                    TaskId("coord"),
                    NOW,
                ),
                work_models.PlannedReplacement(
                    WorkItemId("dep-retained"),
                    1,
                    WorkItemId("replacer"),
                    "Retained cost.",
                    work_models.PlannedReplacementStatus.CURRENT,
                    TaskId("coord"),
                    NOW,
                ),
            ),
            replacement_dispositions=(
                work_models.ReplacementDisposition(
                    WorkItemId("dep-retained"),
                    1,
                    "Keep temporarily.",
                    "Retained cost.",
                    TaskId("coord"),
                    NOW,
                ),
            ),
        )

        self.assertEqual(items_hold, snapshot.releasable_unstarted_hold(unstarted_satisfied))
        self.assertEqual(checkpoint_hold, snapshot.releasable_unstarted_hold(paused_unstarted))
        self.assertEqual(items_hold, snapshot.releasable_unstarted_hold(replacement_resolved))
        for non_releasable in (
            started_satisfied,
            ready_item,
            human_blocked,
            outside_blocked,
            still_waiting,
            cancelled_waiting,
            replacement_withheld,
        ):
            with self.subTest(item=non_releasable.work_item_id):
                self.assertIsNone(snapshot.releasable_unstarted_hold(non_releasable))

    def test_block_item_refuses_satisfied_items_hold_while_attempt_block_and_other_holds_remain_accepted(self) -> None:
        items_hold = work_models.WaitingForItemsToFinish((WorkItemId("survey"),))
        human_hold = work_models.WaitingForHumanDecision("Wait for sign-off?")
        outside_hold = work_models.WaitingForOutsideCondition("Staging environment provisioned.")
        ready_build = work_item("build", work_models.WorkState.READY, depends_on=(WorkItemId("survey"),))

        satisfied_snapshot = LedgerSnapshot(
            "rev-1",
            (ready_build,),
            definitions=(_anchor("build", (WorkItemId("survey"),)),),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )
        rejected = decision_outcome(
            satisfied_snapshot,
            decision_models.BlockWorkItemCommand(
                action(decision_models.BlockWorkItemAction, WorkItemId("build")),
                work_models.BlockInput(items_hold),
            ),
            NOW,
        )
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
        assert rejected.details is not None
        self.assertEqual((), rejected.details.changed_surfaces)
        self.assertIn("already ready to start", rejected.message)

        for non_items in (human_hold, outside_hold):
            with self.subTest(unblocker=non_items):
                accepted = expect_success(
                    decision_outcome(
                        satisfied_snapshot,
                        decision_models.BlockWorkItemCommand(
                            action(decision_models.BlockWorkItemAction, WorkItemId("build")),
                            work_models.BlockInput(non_items),
                        ),
                        NOW,
                    )
                )
                self.assertEqual((), accepted.released_holds)

        withholding_replacement = LedgerSnapshot(
            "rev-1",
            (ready_build,),
            definitions=(_anchor("build", (WorkItemId("survey"),)),),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
            planned_replacements=(
                work_models.PlannedReplacement(
                    WorkItemId("build"),
                    1,
                    WorkItemId("campaign"),
                    "Campaign owns build.",
                    work_models.PlannedReplacementStatus.CURRENT,
                    TaskId("coord"),
                    NOW,
                ),
            ),
        )
        expect_success(
            decision_outcome(
                withholding_replacement,
                decision_models.BlockWorkItemCommand(
                    action(decision_models.BlockWorkItemAction, WorkItemId("build")),
                    work_models.BlockInput(items_hold),
                ),
                NOW,
            )
        )

        active_snapshot = LedgerSnapshot(
            "rev-1",
            (work_item("build", work_models.WorkState.ACTIVE, attempt="build-1", depends_on=(WorkItemId("survey"),)),),
            attempts=(attempt_record("build-1", "build", work_models.AttemptState.ACTIVE, 1, DIGEST),),
            definitions=(_anchor("build", (WorkItemId("survey"),)),),
            attempt_authorities=(
                work_models.AttemptAuthority(AttemptId("build-1"), WorkItemId("build"), LeaseId("lease-1"), 1),
            ),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )
        expect_success(
            decision_outcome(
                active_snapshot,
                decision_models.BlockCommand(
                    action(decision_models.BlockAttemptAction, AttemptId("build-1")),
                    work_models.BlockInput(items_hold),
                ),
                NOW,
            )
        )

    def test_unclassified_unstarted_hold_allows_resume_route_while_prepare_and_started_resume_still_refuse(
        self,
    ) -> None:
        unclassified_unstarted = LedgerSnapshot(
            "rev-1",
            (
                work_item(
                    "legacy-unstarted",
                    work_models.WorkState.BLOCKED,
                    unblocker=work_models.WaitingForHumanDecision("Proceed?"),
                ),
                work_item("legacy-ready", work_models.WorkState.READY),
                work_item(
                    "legacy-started",
                    work_models.WorkState.PAUSED,
                    attempt="legacy-started-1",
                    unblocker=work_models.WaitingForHumanDecision("Proceed?"),
                ),
            ),
            attempts=(
                attempt_record("legacy-started-1", "legacy-started", work_models.AttemptState.PAUSED, 1, DIGEST),
            ),
            definitions=(
                _anchor("legacy-unstarted", classified=False),
                _anchor("legacy-ready", classified=False),
                _anchor("legacy-started", classified=False),
            ),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )

        self.assertIsNone(
            routing.validate_action_route(
                unclassified_unstarted, action(decision_models.ResumeAction, WorkItemId("legacy-unstarted"))
            )
        )
        refused_prepare = routing.validate_action_route(
            unclassified_unstarted, action(decision_models.PrepareAction, WorkItemId("legacy-ready"))
        )
        assert isinstance(refused_prepare, routing.RouteClassificationRequired)
        self.assertEqual(DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED, refused_prepare.failure().code)

        refused_started_resume = routing.validate_action_route(
            unclassified_unstarted, action(decision_models.ResumeAction, WorkItemId("legacy-started"))
        )
        assert isinstance(refused_started_resume, routing.RouteClassificationRequired)
        self.assertEqual(DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED, refused_started_resume.failure().code)


class _HoldReleaseSupport(CheckpointPackageSupport):
    def native_fixture[T: CheckpointFixture](self, fixture: T) -> T:
        native = self.enterContext(native_tools())
        return dataclasses.replace(
            fixture,
            call_native_tool=native.call_native_tool,
            call_advertised_tool=native.call_advertised_tool,
        )

    def roots(self, fixture: CheckpointFixture) -> JsonObject:
        return {"project_root": str(fixture.project), "work_root": str(fixture.work)}

    def create_item(
        self,
        fixture: CheckpointFixture,
        item_id: str,
        *,
        follow_up: str | JsonObject | None = None,
    ) -> None:
        value = proposal()
        value["proposal_id"] = item_id
        if follow_up is not None:
            value["relation"] = {"kind": "follow-up", "item": follow_up}
            related_id = WorkItemId(str(follow_up["item"] if isinstance(follow_up, dict) else follow_up))
            state = fixture.store.validated_snapshot()
            value["related_disposition"] = {
                "kind": "related",
                "item_id": related_id,
                "active_memberships": [
                    initiative.initiative_id
                    for initiative in state.initiatives
                    for member in initiative.memberships
                    if member.item_id == related_id and member.removed is None
                ],
                "active_off_roadmap": any(
                    episode.item_id == related_id and episode.closed is None for episode in state.off_roadmap_episodes
                ),
            }
        result = fixture.call_advertised_tool(
            mcp_server.PROPOSAL_CREATE_TOOL,
            {**self.roots(fixture), "proposal": value, "actor_task_id": "coordinator", "actor_host_id": "local"},
        )
        self.assertEqual("committed", result["status"], result)

    def revise_dependencies(
        self,
        fixture: CheckpointFixture,
        item_id: str,
        dependencies: list[JsonValue],
    ) -> JsonObject:
        current = fixture.call_advertised_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": item_id}},
        )
        definition_json = {**self.json_object(current["definition"]), "dependencies": dependencies}
        result = self.transition_result(
            fixture,
            self.project_action(fixture, f"revise-item:{item_id}"),
            {
                "schema": "pinboard-item-revision/v1",
                "expected_revision": current["definition_revision"],
                "expected_digest": current["definition_digest"],
                "source_task": "coordinator",
                "reason": "Update accepted dependencies.",
                "definition": definition_json,
            },
        )
        self.assertEqual("committed", result["status"], result)
        return result

    def block_unstarted(self, fixture: CheckpointFixture, item_id: str, unblocker: JsonObject) -> JsonObject:
        return self.transition_result(
            fixture,
            self.project_action(fixture, f"block-item:{item_id}"),
            {"unblocker": unblocker},
        )

    def activate_item(self, fixture: CheckpointFixture, item_id: str) -> None:
        prepared = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "start",
                    "item_id": item_id,
                    "task_id": "coordinator",
                    "host_id": "local",
                    "ttl_seconds": 600,
                }
            },
        )
        self.assertEqual("committed", prepared["status"], prepared)
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        base_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=fixture.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        template = work_c_brief()
        local = template.checkpoint
        assert isinstance(local, work_brief_models.LocalCheckpoint)
        brief = replace_struct(
            template,
            attempt_id=f"{item_id}-1",
            item_id=item_id,
            artifact_revision=1,
            branch=branch,
            base_revision=base_revision,
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
                **self.roots(fixture),
                "brief": self.json_object(msgspec.json.decode(msgspec.json.encode(brief))),
            },
        )
        self.assertEqual("committed", published["status"], published)
        activate = self.native_actions(fixture, "activate", item_id, role="preparer", lease=prepared)
        activated = fixture.call_native_tool(
            mcp_server.TRANSITION_TOOL,
            {
                "request": {
                    **self.roots(fixture),
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

    def item_status(self, fixture: CheckpointFixture, item_id: str) -> JsonObject:
        return fixture.call_advertised_tool(
            mcp_server.ITEM_STATUS_TOOL,
            {"request": {**self.roots(fixture), "operation": "item", "item_id": item_id}},
        )

    def cancel_item(self, fixture: CheckpointFixture, item_id: str) -> JsonObject:
        preview = fixture.call_advertised_tool(
            mcp_server.CANCEL_TOOL,
            {"request": {**self.roots(fixture), "operation": "preview", "item_id": item_id}},
        )
        self.assertEqual("ok", preview["status"], preview)
        confirmed = fixture.call_advertised_tool(
            mcp_server.CANCEL_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "confirm",
                    "item_id": item_id,
                    "binding": preview["binding"],
                    "human_confirmation": f"Cancel {item_id} and keep history.",
                    "actor_task_id": "coordinator",
                    "actor_host_id": "local",
                }
            },
        )
        self.assertEqual("committed", confirmed["status"], confirmed)
        return confirmed

    def close_done(self, fixture: CheckpointFixture, item_id: str) -> JsonObject:
        action_json = self.project_action(fixture, f"close:{item_id}")
        closed = fixture.call_advertised_tool(
            mcp_server.CLOSE_TOOL,
            {
                **self.roots(fixture),
                "receipt": {
                    "action_id": action_json["action_id"],
                    "subject_revision": action_json["subject_revision"],
                },
                "payload": {
                    "outcome": "done",
                    "reason": f"Delivered {item_id}.",
                    "human_decision": f"Yes, close {item_id} as done.",
                },
                "actor_task_id": "coordinator",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("committed", closed["status"], closed)
        return closed

    def record_amendment(self, fixture: CheckpointFixture, attempt_id: str) -> JsonObject:
        selected = self.project_action(fixture, f"record-amendment:{attempt_id}")
        recorded = self.transition_result(
            fixture,
            selected,
            {
                "schema": "pinboard-attempt-amendment/v1",
                "amendment_class": "stale-documentation",
                "change": "Align INSTALL.md authority note.",
                "reason": "Keep installation note current.",
                "human_decision": "Yes, fold that note into this attempt.",
            },
        )
        self.assertEqual("committed", recorded["status"], recorded)
        return recorded

    def setup_sibling_dependents(self, fixture: CheckpointFixture, prereq_id: str) -> None:
        self.create_item(fixture, "other-live")
        self.create_item(fixture, "replacer")
        self.create_item(fixture, "cancelled-prereq")
        self.create_item(fixture, "unstarted-ready-dep", follow_up=prereq_id)
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "unstarted-ready-dep", {"kind": "items", "items": [prereq_id]})["status"],
        )

        self.create_item(fixture, "started-dep")
        self.activate_item(fixture, "started-dep")
        self.record_amendment(fixture, "started-dep-1")
        self.revise_dependencies(fixture, "started-dep", [prereq_id])
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "block:started-dep-1"),
                {"unblocker": {"kind": "items", "items": [prereq_id]}},
            )["status"],
        )

        self.create_item(fixture, "two-deps")
        self.revise_dependencies(fixture, "two-deps", [prereq_id, "other-live"])
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "two-deps", {"kind": "items", "items": [prereq_id]})["status"],
        )

        self.create_item(fixture, "replacement-withheld", follow_up=prereq_id)
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "record-replacement:replacement-withheld"),
                {
                    "schema": "pinboard-planned-replacement/v1",
                    "expected_relation_revision": 0,
                    "replacement_item": "replacer",
                    "replacement_cost": "Replacer supersedes replacement-withheld.",
                    "status": "current",
                    "recorded_by": "coordinator",
                },
            )["status"],
        )
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "replacement-withheld", {"kind": "items", "items": [prereq_id]})["status"],
        )

        self.create_item(fixture, "cancelled-dep")
        self.revise_dependencies(fixture, "cancelled-dep", [prereq_id, "cancelled-prereq"])
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "cancelled-dep", {"kind": "items", "items": [prereq_id, "cancelled-prereq"]})[
                "status"
            ],
        )
        self.cancel_item(fixture, "cancelled-prereq")

        self.create_item(fixture, "human-hold", follow_up=prereq_id)
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "human-hold", {"kind": "human-decision", "question": "Approve rollout?"})[
                "status"
            ],
        )

        self.create_item(fixture, "outside-hold", follow_up=prereq_id)
        self.assertEqual(
            "committed",
            self.block_unstarted(
                fixture, "outside-hold", {"kind": "outside-condition", "condition": "Upstream tag exists."}
            )["status"],
        )

    def assert_mixed_sibling_release(
        self,
        fixture: CheckpointFixture,
        history_id: int,
        expected_waited: tuple[str, ...],
    ) -> None:
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        states = {str(item.item_id): item.state.value for item in fresh.lifecycle.work_items}
        self.assertEqual("ready", states["unstarted-ready-dep"])
        for kept in (
            "started-dep",
            "two-deps",
            "replacement-withheld",
            "cancelled-dep",
            "human-hold",
            "outside-hold",
        ):
            self.assertEqual("blocked", states[kept], kept)
        self.assertNotIn(
            WorkItemId("unstarted-ready-dep"),
            {entry.item_id for entry in fresh.lifecycle.unblockers},
        )
        matching = [row for row in fresh.lifecycle.released_holds if int(row.history_id) == history_id]
        self.assertEqual(1, len(matching))
        self.assertEqual(WorkItemId("unstarted-ready-dep"), matching[0].item_id)
        self.assertEqual(stored_state.StoredWorkItemState.BLOCKED, matching[0].previous_state)
        self.assertEqual(
            work_models.WaitingForItemsToFinish(tuple(WorkItemId(value) for value in expected_waited)),
            matching[0].unblocker,
        )

        history_view = (fixture.work / "views" / "history" / f"{history_id}.md").read_text(encoding="utf-8")
        item_view = (fixture.work / "views" / "items" / "unstarted-ready-dep.md").read_text(encoding="utf-8")
        self.assertIn("## Released holds", history_view)
        self.assertIn("[unstarted-ready-dep](../items/unstarted-ready-dep.md)", history_view)
        self.assertIn("## Released holds", item_view)
        self.assertIn(f"Receipt [{history_id}](../history/{history_id}.md)", item_view)

        started_hold = self.json_object(self.item_status(fixture, "started-dep")["hold"])
        self.assertTrue(started_hold["resume_needed"])
        self.assertIn("can continue once someone resumes it", str(started_hold["sentence"]))
        self.assertEqual(
            "ok",
            self.actions_result(
                fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "started-dep"}}
            )["status"],
        )

        two_deps_hold = self.json_object(self.item_status(fixture, "two-deps")["hold"])
        self.assertFalse(two_deps_hold["resume_needed"])
        self.assertIn("other-live to finish", str(two_deps_hold["sentence"]))

        replacement_hold = self.json_object(self.item_status(fixture, "replacement-withheld")["hold"])
        self.assertFalse(replacement_hold["resume_needed"])
        self.assertIn("planned replacement by replacer to be resolved", str(replacement_hold["sentence"]))

        cancelled_hold = self.json_object(self.item_status(fixture, "cancelled-dep")["hold"])
        self.assertFalse(cancelled_hold["resume_needed"])
        self.assertIn("cancelled-prereq was cancelled and will not finish", str(cancelled_hold["sentence"]))

        for manual_id in ("human-hold", "outside-hold"):
            manual_hold = self.json_object(self.item_status(fixture, manual_id)["hold"])
            self.assertFalse(manual_hold["resume_needed"])

        views_before = {
            path: path.read_bytes() for path in sorted((fixture.work / "views").rglob("*")) if path.is_file()
        }
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (stdout, stderr))
        self.assertEqual(views_before, {path: path.read_bytes() for path in views_before})
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported = msgspec.json.decode(
            self.run_cli(*fixture.common, "export", "--json")[1],
            type=project_export.ProjectExport,
            strict=True,
        )
        self.assertEqual(("pinboard-project-export/v8", "sqlite-v16"), (exported.schema, exported.authority))
        self.assertTrue(any(row.attempt_id == "started-dep-1" for row in exported.amendments))
        self.assertTrue(
            any(
                row.history_id == history_id and row.item_id == "unstarted-ready-dep" for row in exported.released_holds
            )
        )


class AutomaticHoldReleasePersistenceTest(_HoldReleaseSupport):
    def _submitted_review_fixture(self) -> tuple[AcceptedPackageFixture, str]:
        fixture = self.native_fixture(self.accepted_package_fixture(local=True, candidate_form="current-head"))
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("UPDATE work_items SET state = 'active' WHERE item_id = ?", (fixture.brief.item_id,))
            sync_test_unblocker(connection, fixture.brief.item_id)
            connection.execute("UPDATE attempts SET state = 'active' WHERE attempt_id = ?", (fixture.brief.attempt_id,))
            move_item_state_count(
                connection, stored_state.StoredWorkItemState.PAUSED, stored_state.StoredWorkItemState.ACTIVE
            )
        attempt_root = fixture.work / "attempts" / fixture.brief.attempt_id
        (attempt_root / "result.md").write_bytes(b"terminal result\n")
        (attempt_root / "review.md").write_bytes(b"terminal independent review\n")
        candidate = self.submit_review(fixture, "commissioned candidate", "commissioned-worker")
        return fixture, candidate

    def _run_pr_review_close(self, fixture: CheckpointFixture, item_id: str, outcome: str) -> int:
        current = next(
            value
            for value in reversed(fixture.store.validated_snapshot().lifecycle.definition_revisions)
            if str(value.item_id) == item_id
        )
        subject_revision = next(
            value.subject_revision
            for value in fixture.store.validated_snapshot().lifecycle.work_items
            if str(value.item_id) == item_id
        )
        started = fixture.call_advertised_tool(
            mcp_server.PR_REVIEW_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "start",
                    "item_id": item_id,
                    "expected_subject_revision": subject_revision,
                    "actor_task_id": "preparer",
                    "actor_host_id": "local",
                    "brief": msgspec.to_builtins(
                        pr_reviews.ReviewBrief(
                            "pinboard-pr-review-brief/v1",
                            item_id,
                            current.revision,
                            current.digest,
                            "https://github.com/example/repo/pull/99",
                            "author",
                            (pr_reviews.Requirement("issue:99", "Behavior holds.", "MCP", "application"),),
                            ("Unit tests",),
                            "preparer",
                        )
                    ),
                }
            },
        )
        self.assertEqual("committed", started["status"], started)
        brief_history_id = started["brief_history_id"]
        assert isinstance(brief_history_id, int)
        approved = fixture.call_advertised_tool(
            mcp_server.PR_REVIEW_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "review-brief",
                    "item_id": item_id,
                    "expected_subject_revision": started["subject_revision"],
                    "actor_task_id": "brief-reviewer",
                    "actor_host_id": "local",
                    "brief_review": msgspec.to_builtins(
                        pr_reviews.BriefReview(
                            "pinboard-pr-review-brief-review/v1",
                            item_id,
                            brief_history_id,
                            "brief-reviewer",
                            "ready",
                            "Verified.",
                        )
                    ),
                }
            },
        )
        self.assertEqual("committed", approved["status"], approved)
        closed = fixture.call_advertised_tool(
            mcp_server.PR_REVIEW_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "close",
                    "item_id": item_id,
                    "expected_subject_revision": approved["subject_revision"],
                    "actor_task_id": "human-owner",
                    "actor_host_id": "local",
                    "close": {
                        "schema": "pinboard-pr-review-close/v1",
                        "item_id": item_id,
                        "final_round_history_id": None,
                        "last_reviewed_head": None,
                        "newer_observed_head": None,
                        "newer_observation_source": None,
                        "final_dispositions": [],
                        "human_direction": f"Close PR review as {outcome}.",
                        "human_task_id": "human-owner",
                        "outcome": outcome,
                    },
                }
            },
        )
        self.assertEqual("committed", closed["status"], closed)
        return int(
            SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot().transition_receipts[-1].history_id
        )

    def _completion_payload(self, fixture: AcceptedPackageFixture, candidate: str, reviewer: str) -> JsonObject:
        state = fixture.store.validated_snapshot()
        checkpoint = next(
            value
            for value in state.transition_receipts
            if value.outcome_schema == "checkpoint-acceptance/v2" and str(value.subject_id) == fixture.brief.attempt_id
        )
        attempt_root = fixture.work / "attempts" / fixture.brief.attempt_id
        return {
            "schema": "pinboard-reviewed-completion/v3",
            "human_decision": "Complete this item with its reviewed outcome.",
            "candidate": candidate,
            "evidence": "The commissioned reviewer accepted the exact candidate.",
            "reviewer_task_id": reviewer,
            "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
            "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
            "packages": [
                {
                    "history_id": int(checkpoint.history_id),
                    "package_sha256": fixture.package_reference.content_sha256,
                    "disposition": "revalidated",
                    "evidence": "The final relationship was rechecked.",
                }
            ],
        }

    def test_complete_close_pr_review_and_checkpoint_acceptance_release_only_releasable_unstarted_holds(  # noqa: PLR0915 - one persisted trigger and sibling-exclusion matrix
        self,
    ) -> None:
        with self.subTest(trigger="complete"):
            fixture, candidate = self._submitted_review_fixture()
            fixture = self.terminalize_brief(fixture)
            self.setup_sibling_dependents(fixture, "work-a")
            self.record_commissioned_review(fixture, candidate, "commissioned-reviewer")
            completed = self.transition_result(
                fixture,
                self.project_action(fixture, "complete:work-a-1"),
                self._completion_payload(fixture, candidate, "commissioned-reviewer"),
            )
            self.assertEqual("committed", completed["status"], completed)
            self.assert_mixed_sibling_release(fixture, int(str(completed["history_id"])), ("work-a",))

        with self.subTest(trigger="close-done"):
            fixture = self.native_fixture(self.checkpoint_fixture(local=True))
            self.create_item(fixture, "prereq")
            self.setup_sibling_dependents(fixture, "prereq")
            closed = self.close_done(fixture, "prereq")
            self.assert_mixed_sibling_release(fixture, int(str(closed["history_id"])), ("prereq",))

        with self.subTest(trigger="pr-review-accepted"):
            fixture = self.native_fixture(self.checkpoint_fixture(local=True))
            self.create_item(fixture, "pr-prereq")
            self.setup_sibling_dependents(fixture, "pr-prereq")
            history_id = self._run_pr_review_close(fixture, "pr-prereq", "accepted")
            self.assert_mixed_sibling_release(fixture, history_id, ("pr-prereq",))

        with self.subTest(trigger="pr-review-stopped-and-cancel-do-not-release"):
            fixture = self.native_fixture(self.checkpoint_fixture(local=True))
            self.create_item(fixture, "stopped-pr")
            self.create_item(fixture, "cancelled-work")
            self.create_item(fixture, "waits-on-stopped", follow_up="stopped-pr")
            self.create_item(fixture, "waits-on-cancelled", follow_up="cancelled-work")
            self.assertEqual(
                "committed",
                self.block_unstarted(fixture, "waits-on-stopped", {"kind": "items", "items": ["stopped-pr"]})["status"],
            )
            self.assertEqual(
                "committed",
                self.block_unstarted(fixture, "waits-on-cancelled", {"kind": "items", "items": ["cancelled-work"]})[
                    "status"
                ],
            )
            self._run_pr_review_close(fixture, "stopped-pr", "stopped")
            self.cancel_item(fixture, "cancelled-work")
            fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
            self.assertEqual((), fresh.lifecycle.released_holds)
            for dependent_id, cancelled_target in (
                ("waits-on-stopped", "stopped-pr"),
                ("waits-on-cancelled", "cancelled-work"),
            ):
                status = self.item_status(fixture, dependent_id)
                self.assertEqual("blocked", status["state"])
                hold = self.json_object(status["hold"])
                self.assertFalse(hold["resume_needed"])
                self.assertIn(f"{cancelled_target} was cancelled and will not finish", str(hold["sentence"]))
                self.assertEqual(
                    "rejected",
                    self.actions_result(
                        fixture, {"role": "project", "action_id": {"kind": "resume", "subject": dependent_id}}
                    )["status"],
                )

        with self.subTest(trigger="accept-checkpoint"):
            fixture = self.native_fixture(self.checkpoint_fixture(local=True))
            checkpoint = fixture.brief.checkpoint.checkpoint_id
            self.create_item(fixture, "cp-unstarted", follow_up={"item": "work-a", "checkpoint": checkpoint})
            self.create_item(fixture, "whole-unstarted", follow_up="work-a")
            self.assertEqual(
                "committed",
                self.block_unstarted(fixture, "cp-unstarted", {"kind": "items", "items": ["work-a"]})["status"],
            )
            self.assertEqual(
                "committed",
                self.block_unstarted(fixture, "whole-unstarted", {"kind": "items", "items": ["work-a"]})["status"],
            )
            accepted = self.transition_result(
                fixture,
                self.project_action(fixture, "accept-checkpoint:work-a-1"),
                self.json_object(json.loads(fixture.payload.read_bytes())),
            )
            self.assertEqual("committed", accepted["status"], accepted)
            cp_history_id = int(str(accepted["history_id"]))
            fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
            states = {str(item.item_id): item.state.value for item in fresh.lifecycle.work_items}
            self.assertEqual("ready", states["cp-unstarted"])
            self.assertEqual("blocked", states["whole-unstarted"])
            self.assertEqual(
                (WorkItemId("cp-unstarted"),),
                tuple(row.item_id for row in fresh.lifecycle.released_holds if int(row.history_id) == cp_history_id),
            )

    def test_merge_proposal_revise_item_and_replacement_resolutions_release_unstarted_holds_in_same_transaction(
        self,
    ) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "accept-checkpoint:work-a-1"),
                self.json_object(json.loads(fixture.payload.read_bytes())),
            )["status"],
        )

        self.create_item(fixture, "done-target")
        self.close_done(fixture, "done-target")
        refused_block = self.block_unstarted(
            fixture,
            "work-c",
            {"kind": "items", "items": ["done-target"]},
        )
        self.assertEqual(("rejected", "TRANSITION_INPUT_INVALID"), (refused_block["status"], refused_block["code"]))

        self.create_item(fixture, "waits-on-proposal", follow_up="zz-proposal-a")
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "waits-on-proposal", {"kind": "items", "items": ["zz-proposal-a"]})["status"],
        )
        merged = self.transition_result(
            fixture,
            self.project_action(fixture, "merge-proposal:zz-proposal-a"),
            {"target": "done-target"},
        )
        self.assertEqual("committed", merged["status"], merged)
        self.assertEqual(
            ("ready", None),
            (
                self.item_status(fixture, "waits-on-proposal")["state"],
                self.item_status(fixture, "waits-on-proposal")["hold"],
            ),
        )

        self.create_item(fixture, "live-dep")
        self.create_item(fixture, "revise-remove")
        self.revise_dependencies(fixture, "revise-remove", ["done-target", "live-dep"])
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "revise-remove", {"kind": "items", "items": ["done-target", "live-dep"]})[
                "status"
            ],
        )
        removed = self.revise_dependencies(fixture, "revise-remove", ["done-target"])
        self.assertEqual("ready", self.item_status(fixture, "revise-remove")["state"])
        self.assertIn(
            "## Released holds",
            (fixture.work / "views" / "history" / f"{removed['history_id']}.md").read_text(encoding="utf-8"),
        )

        self.create_item(fixture, "revise-narrow", follow_up="work-a")
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "revise-narrow", {"kind": "items", "items": ["work-a"]})["status"],
        )
        narrowed = self.revise_dependencies(fixture, "revise-narrow", [{"item": "work-a", "checkpoint": checkpoint}])
        self.assertEqual("ready", self.item_status(fixture, "revise-narrow")["state"])
        self.assertIn(
            "## Released holds",
            (fixture.work / "views" / "history" / f"{narrowed['history_id']}.md").read_text(encoding="utf-8"),
        )

        self.create_item(fixture, "replacer-item")
        for item_id, mode in (("withdraw-target", "withdraw"), ("retain-target", "retain")):
            with self.subTest(mode=mode):
                self.create_item(fixture, item_id, follow_up="done-target")
                self.assertEqual(
                    "committed",
                    self.transition_result(
                        fixture,
                        self.project_action(fixture, f"record-replacement:{item_id}"),
                        {
                            "schema": "pinboard-planned-replacement/v1",
                            "expected_relation_revision": 0,
                            "replacement_item": "replacer-item",
                            "replacement_cost": "Replaced by replacer-item.",
                            "status": "current",
                            "recorded_by": "coordinator",
                        },
                    )["status"],
                )
                self.assertEqual(
                    "committed",
                    self.block_unstarted(fixture, item_id, {"kind": "items", "items": ["done-target"]})["status"],
                )
                if mode == "withdraw":
                    resolved = self.transition_result(
                        fixture,
                        self.project_action(fixture, f"record-replacement:{item_id}"),
                        {
                            "schema": "pinboard-planned-replacement/v1",
                            "expected_relation_revision": 1,
                            "replacement_item": "replacer-item",
                            "replacement_cost": "Replaced by replacer-item.",
                            "status": "withdrawn",
                            "recorded_by": "coordinator",
                        },
                    )
                else:
                    resolved = self.transition_result(
                        fixture,
                        self.project_action(fixture, f"retain-temporarily:{item_id}"),
                        {
                            "schema": "pinboard-replacement-disposition/v1",
                            "relation_revision": 1,
                            "rationale": "Deliver urgently before replacer-item.",
                            "accepted_cost": "Replaced by replacer-item.",
                            "recorded_by": "coordinator",
                        },
                    )
                self.assertEqual("committed", resolved["status"], resolved)
                self.assertEqual("ready", self.item_status(fixture, item_id)["state"])

        self.create_item(fixture, "started-self")
        self.activate_item(fixture, "started-self")
        self.revise_dependencies(fixture, "started-self", ["done-target", "live-dep"])
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "block:started-self-1"),
                {"unblocker": {"kind": "items", "items": ["done-target", "live-dep"]}},
            )["status"],
        )
        self.revise_dependencies(fixture, "started-self", ["done-target"])
        self.assertEqual("blocked", self.item_status(fixture, "started-self")["state"])
        self.assertTrue(self.json_object(self.item_status(fixture, "started-self")["hold"])["resume_needed"])

        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(
            {
                WorkItemId("waits-on-proposal"),
                WorkItemId("revise-remove"),
                WorkItemId("revise-narrow"),
                WorkItemId("withdraw-target"),
                WorkItemId("retain-target"),
            },
            {entry.item_id for entry in fresh.lifecycle.released_holds},
        )
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

    def test_consolidate_v3_previews_and_releases_unstarted_holds_and_retains_v1_and_v2_receipt_compatibility(  # noqa: PLR0915 - one persisted consolidation v3 and historical v1/v2 compatibility journey
        self,
    ) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "accept-checkpoint:work-a-1"),
                self.json_object(json.loads(fixture.payload.read_bytes())),
            )["status"],
        )

        self.create_item(fixture, "source-a")
        self.create_item(fixture, "held-consumer", follow_up="source-a")
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "held-consumer", {"kind": "items", "items": ["source-a"]})["status"],
        )
        current_campaign = fixture.call_advertised_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {"request": {**self.roots(fixture), "operation": "current", "item_id": "work-a"}},
        )
        campaign_def: JsonObject = {
            **self.json_object(current_campaign["definition"]),
            "checkpoints": [checkpoint, "later-stage"],
            "route_facts": {
                **self.json_object(self.json_object(current_campaign["definition"])["route_facts"]),
                "size_class": "campaign",
                "declares_checkpoints": True,
            },
        }
        selection: JsonObject = {
            "sources": [{"item_id": "source-a", "replacement_cost": "Campaign work-a absorbs source-a."}],
            "campaign": {"item_id": "work-a", "create": False, "definition": campaign_def},
            "checkpoint_targets": [
                {
                    "consumer": "held-consumer",
                    "checkpoint": checkpoint,
                    "covers_carried_work": "Accepted checkpoint covers source-a.",
                }
            ],
            "membership_intent": {"kind": "no-initiatives"},
        }
        preview_res = fixture.call_advertised_tool(
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
        self.assertEqual("ok", preview_res["status"], preview_res)
        self.assertEqual("pinboard-consolidation-preview-result/v3", preview_res["schema"])
        preview_obj = self.json_object(preview_res["preview"])
        binding_obj = self.json_object(preview_res["binding"])
        self.assertEqual("pinboard-consolidation-preview/v3", preview_obj["schema"])
        self.assertEqual("pinboard-consolidation-binding/v3", binding_obj["schema"])
        held_subject_revision = next(
            value.subject_revision
            for value in fixture.store.validated_snapshot().lifecycle.work_items
            if str(value.item_id) == "held-consumer"
        )
        self.assertEqual(
            [
                {
                    "item_id": "held-consumer",
                    "previous_state": "blocked",
                    "previous_subject_revision": held_subject_revision,
                    "unblocker": {"kind": "items", "items": ["source-a"]},
                }
            ],
            preview_obj["released_holds"],
        )
        self.assertEqual(
            consolidation_models.RELEASED_LIFECYCLE_EFFECT,
            str(preview_obj["lifecycle_effect"]),
        )

        confirmed = fixture.call_advertised_tool(
            mcp_server.CONSOLIDATE_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "confirm",
                    "selection": selection,
                    "binding": binding_obj,
                    "human_confirmation": "Confirm consolidation and release satisfied holds.",
                    "actor_task_id": "human",
                    "actor_host_id": "local",
                }
            },
        )
        self.assertEqual("committed", confirmed["status"], confirmed)
        history_id = int(str(confirmed["history_id"]))
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual("ready", self.item_status(fixture, "held-consumer")["state"])
        self.assertEqual(
            (WorkItemId("held-consumer"),),
            tuple(entry.item_id for entry in fresh.lifecycle.released_holds if int(entry.history_id) == history_id),
        )
        self.assertIn(
            "## Released holds",
            (fixture.work / "views" / "history" / f"{history_id}.md").read_text(encoding="utf-8"),
        )
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

        receipt = fresh.transition_receipts[-1]
        v3_record = msgspec.json.decode(bytes(receipt.input_payload), type=consolidation_models.Record)
        v2_preview = msgspec.to_builtins(v3_record.preview)
        assert isinstance(v2_preview, dict)
        v2_preview.pop("released_holds")
        v2_preview["schema"] = "pinboard-consolidation-preview/v2"
        v2_preview["lifecycle_effect"] = consolidation_models.PRESERVED_LIFECYCLE_EFFECT
        v2_preview_bytes = msgspec.json.encode(v2_preview, order="sorted")
        v2_record = {
            "schema": "pinboard-consolidation/v2",
            "preview": v2_preview,
            "binding": {
                "schema": "pinboard-consolidation-binding/v2",
                "work_root": v3_record.binding.work_root,
                "preview_sha256": hashlib.sha256(v2_preview_bytes).hexdigest(),
            },
            "human_confirmation": v3_record.human_confirmation,
        }
        v2_bytes = msgspec.json.encode(v2_record, order="sorted").decode("utf-8")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute("DELETE FROM released_item_holds WHERE history_id = ?", (history_id,))
            connection.execute(
                "UPDATE transition_history SET input_schema = 'pinboard-consolidation/v2', input_json = ? WHERE history_id = ?",
                (v2_bytes, history_id),
            )
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (stdout, stderr))
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported = self.run_json_cli(*fixture.common, "export")
        self.assertIn("pinboard-consolidation/v2", str(exported))

        v1_preview = dict(v2_preview)
        v1_preview["schema"] = "pinboard-consolidation-preview/v1"
        v1_preview.pop("source_dispositions", None)
        v1_preview_bytes = msgspec.json.encode(v1_preview, order="sorted")
        v1_record = {
            "schema": "pinboard-consolidation/v1",
            "preview": v1_preview,
            "binding": {
                "schema": "pinboard-consolidation-binding/v1",
                "work_root": v3_record.binding.work_root,
                "preview_sha256": hashlib.sha256(v1_preview_bytes).hexdigest(),
            },
            "human_confirmation": v3_record.human_confirmation,
        }
        v1_bytes = msgspec.json.encode(v1_record, order="sorted").decode("utf-8")
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE transition_history SET input_schema = 'pinboard-consolidation/v1', input_json = ? WHERE history_id = ?",
                (v1_bytes, history_id),
            )
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        exported_v1 = self.run_json_cli(*fixture.common, "export")
        self.assertIn("pinboard-consolidation/v1", str(exported_v1))


class UnclassifiedUnstartedResumeTest(_HoldReleaseSupport):
    def _strip_route_facts_to_v3(
        self, fixture: CheckpointFixture, item_id: str, *, attempt_id: str | None = None
    ) -> None:
        current = next(
            value
            for value in fixture.store.validated_snapshot().lifecycle.definition_revisions
            if str(value.item_id) == item_id and value.revision == 1
        )
        assert isinstance(current.definition, work_models.WorkItemDefinition)
        unclassified = dataclasses.replace(current.definition, checkpoints=(), route_facts=None)
        encoded = expect_success(history.work_item_definition_bytes(unclassified))
        legacy_digest = hashlib.sha256(encoded).hexdigest()
        with contextlib.closing(sqlite3.connect(fixture.work / "state.sqlite3")) as connection, connection:
            connection.execute(
                "UPDATE work_item_definition_revisions SET definition_json = ?, definition_digest = ?, after_digest = ? WHERE item_id = ? AND definition_revision = 1",
                (encoded, legacy_digest, legacy_digest, item_id),
            )
            if attempt_id is not None:
                connection.execute(
                    "UPDATE attempts SET accepted_scope_digest = ? WHERE attempt_id = ?",
                    (legacy_digest, attempt_id),
                )

    def test_unclassified_unstarted_hold_resumes_to_ready_while_prepare_and_started_resume_require_classification(
        self,
    ) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        self.create_item(fixture, "unclassified-unstarted")
        self.assertEqual(
            "committed",
            self.block_unstarted(
                fixture,
                "unclassified-unstarted",
                {"kind": "human-decision", "question": "Resume legacy unclassified item?"},
            )["status"],
        )
        self._strip_route_facts_to_v3(fixture, "unclassified-unstarted")
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (stdout, stderr))

        broad = self.actions_result(fixture, {"role": "project"})
        self.assertEqual("ok", broad["status"], broad)
        broad_ids = [self.json_object(self.json_object(row)["action_id"]) for row in self.json_array(broad["actions"])]
        self.assertIn({"kind": "resume", "subject": "unclassified-unstarted"}, broad_ids)

        discovered = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "unclassified-unstarted"}}
        )
        self.assertEqual("ok", discovered["status"], discovered)
        resume_action = self.json_object(self.json_array(discovered["actions"])[0])
        self.assertIsNotNone(resume_action["input_contract"])
        self.assertIsNone(self.json_object(resume_action["instructions"])["route_facts"])
        resumed = self.transition_result(fixture, resume_action, {})
        self.assertEqual("committed", resumed["status"], resumed)
        self.assertEqual("ready", self.item_status(fixture, "unclassified-unstarted")["state"])

        broad_after = self.actions_result(fixture, {"role": "project"})
        broad_after_ids = [
            self.json_object(self.json_object(row)["action_id"]) for row in self.json_array(broad_after["actions"])
        ]
        self.assertNotIn({"kind": "prepare", "subject": "unclassified-unstarted"}, broad_after_ids)

        refused_prep = fixture.call_advertised_tool(
            mcp_server.PREPARATION_AUTHORITY_TOOL,
            {
                "request": {
                    **self.roots(fixture),
                    "operation": "start",
                    "item_id": "unclassified-unstarted",
                    "task_id": "preparer",
                    "host_id": "local",
                    "ttl_seconds": 300,
                }
            },
        )
        self.assertEqual(
            ("rejected", "ROUTE_CLASSIFICATION_REQUIRED"),
            (refused_prep["status"], refused_prep["code"]),
        )

        self.create_item(fixture, "started-legacy")
        self.activate_item(fixture, "started-legacy")
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "pause:started-legacy-1"),
                {
                    "reason": "Pause before removing route facts.",
                    "unblocker": {"kind": "human-decision", "question": "Proceed?"},
                },
            )["status"],
        )
        self._strip_route_facts_to_v3(fixture, "started-legacy", attempt_id="started-legacy-1")
        refused_started = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "started-legacy"}}
        )
        self.assertEqual(
            ("rejected", "ROUTE_CLASSIFICATION_REQUIRED"),
            (refused_started["status"], refused_started["code"]),
        )


class BoardAndOverviewResumeReadinessTest(_HoldReleaseSupport):
    def test_board_ready_to_resume_group_and_overview_v10_resume_needed_across_all_live_states_and_paging(  # noqa: PLR0915 - one complete board group and paged overview v10 journey
        self,
    ) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        self.create_item(fixture, "done-dep")
        self.close_done(fixture, "done-dep")
        self.create_item(fixture, "live-dep")

        self.create_item(fixture, "resumable-blocked")
        self.activate_item(fixture, "resumable-blocked")
        self.revise_dependencies(fixture, "resumable-blocked", ["done-dep"])
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "block:resumable-blocked-1"),
                {"unblocker": {"kind": "items", "items": ["done-dep"]}},
            )["status"],
        )

        self.create_item(fixture, "resumable-paused")
        self.activate_item(fixture, "resumable-paused")
        self.revise_dependencies(fixture, "resumable-paused", ["done-dep"])
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "pause:resumable-paused-1"),
                {
                    "reason": "Paused at boundary.",
                    "unblocker": {"kind": "items", "items": ["done-dep"]},
                },
            )["status"],
        )

        self.create_item(fixture, "paused-human")
        self.activate_item(fixture, "paused-human")
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.project_action(fixture, "pause:paused-human-1"),
                {
                    "reason": "Need product call.",
                    "unblocker": {"kind": "human-decision", "question": "Ship v1 or v2?"},
                },
            )["status"],
        )

        self.create_item(fixture, "active-item")
        self.activate_item(fixture, "active-item")

        self.create_item(fixture, "blocked-waiting", follow_up="live-dep")
        self.assertEqual(
            "committed",
            self.block_unstarted(fixture, "blocked-waiting", {"kind": "items", "items": ["live-dep"]})["status"],
        )
        self.create_item(fixture, "blocked-outside")
        self.assertEqual(
            "committed",
            self.block_unstarted(
                fixture,
                "blocked-outside",
                {"kind": "outside-condition", "condition": "Certificate renewed."},
            )["status"],
        )

        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, (stdout, stderr))
        markdown = (fixture.work / "views" / "board.md").read_text(encoding="utf-8")
        html = (fixture.work / "views" / "board.html").read_text(encoding="utf-8")
        self.assertIn(views_module.NOTICE, markdown)
        headings = [line.removeprefix("## ") for line in markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(
            ["Paused or in review", "Ready to resume", "In progress", "Ready", "Blocked"],
            headings,
        )

        match = re.search(r'<script id="board-data" type="application/json">(.*?)</script>', html, re.DOTALL)
        assert match is not None
        board_data = self.json_object(json.loads(match.group(1)))
        self.assertEqual(
            ["Paused or in review", "Ready to resume", "In progress", "Ready", "Blocked"],
            board_data["groups"],
        )
        self.assertEqual(["paused", "review", "active", "ready", "blocked"], board_data["states"])
        groups_by_item = {
            str(self.json_object(row)["item_id"]): str(self.json_object(row)["group"])
            for row in self.json_array(board_data["items"])
        }
        self.assertEqual("Paused or in review", groups_by_item["work-a"])
        self.assertEqual("Paused or in review", groups_by_item["paused-human"])
        self.assertEqual("Ready to resume", groups_by_item["resumable-blocked"])
        self.assertEqual("Ready to resume", groups_by_item["resumable-paused"])
        self.assertEqual("In progress", groups_by_item["active-item"])
        self.assertEqual("Ready", groups_by_item["live-dep"])
        self.assertEqual("Blocked", groups_by_item["blocked-waiting"])
        self.assertEqual("Blocked", groups_by_item["blocked-outside"])

        seen_resume_needed: dict[str, bool] = {}
        continuation: JsonValue = None
        while True:
            page = fixture.call_advertised_tool(
                mcp_server.OVERVIEW_TOOL,
                {**self.roots(fixture), "max_items": 3, "continuation": continuation},
            )
            self.assertEqual(("pinboard-overview/v10", "sqlite-v16"), (page["schema"], page["authority"]))
            self.assertLessEqual(len(msgspec.json.encode(page)), 8000)
            for raw in self.json_array(page["items"]):
                row = self.json_object(raw)
                seen_resume_needed[str(row["item_id"])] = bool(row["resume_needed"])
            continuation = page["continuation"]
            if continuation is None:
                break

        self.assertEqual(
            {item_id for item_id, flag in seen_resume_needed.items() if flag},
            {"resumable-blocked", "resumable-paused"},
        )


class AutomaticHoldReleaseValidationTest(_HoldReleaseSupport):
    def test_validate_and_export_reject_stuck_releasable_unstarted_holds_and_malformed_release_rows(self) -> None:
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        self.create_item(fixture, "done-dep")
        self.close_done(fixture, "done-dep")
        self.create_item(fixture, "stuck-unstarted", follow_up="done-dep")
        path = fixture.work / "state.sqlite3"
        clean_bytes = path.read_bytes()

        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE work_items SET state = 'blocked' WHERE item_id = 'stuck-unstarted'")
            move_item_state_count(
                connection, stored_state.StoredWorkItemState.READY, stored_state.StoredWorkItemState.BLOCKED
            )
            connection.execute(
                "INSERT INTO work_item_unblockers (item_id, unblocker_json) VALUES (?, ?)",
                ("stuck-unstarted", '{"items":["done-dep"],"kind":"items"}'),
            )
        with self.assertRaises(StorageError) as stuck_err:
            SQLiteWorkStore(path).validated_snapshot()
        self.assertIn("stuck-unstarted", str(stuck_err.exception))
        with self.assertRaises(StorageError) as export_err:
            SQLiteWorkStore(path).read_project_export_batches()
        self.assertIn("stuck-unstarted", str(export_err.exception))
        validate_res = self.run_json_cli_allow_error(*fixture.common, "validate")
        self.assertFalse(validate_res["valid"])

        path.write_bytes(clean_bytes)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            latest_id = connection.execute("SELECT MAX(history_id) FROM transition_history").fetchone()[0]
            connection.execute(
                """INSERT INTO released_item_holds (
                       history_id, item_id, previous_state, previous_subject_revision, unblocker_json
                   ) VALUES (?, ?, ?, ?, ?)""",
                (latest_id, "stuck-unstarted", "blocked", 1, '{"kind":"human-decision","question":"Invalid?"}'),
            )
        with self.assertRaises(StorageError):
            SQLiteWorkStore(path).validated_snapshot()
        with self.assertRaises(StorageError):
            SQLiteWorkStore(path).read_project_export_batches()
        validate_malformed = self.run_json_cli_allow_error(*fixture.common, "validate")
        self.assertFalse(validate_malformed["valid"])

    def run_json_cli_allow_error(self, *arguments: str) -> JsonObject:
        _code, stdout, stderr = self.run_cli(*arguments, "--json")
        self.assertTrue(stdout, stderr)
        return self.json_object(json.loads(stdout))


if __name__ == "__main__":
    unittest.main()
