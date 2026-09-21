"""Current native transition receipts, view repair and bounded follow-up reads."""

import contextlib
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime
from unittest.mock import patch

import msgspec
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from msgspec.structs import replace as replace_struct

from pinboard.adapters import lifecycle_operations
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import work_brief_models, work_briefs
from pinboard.application.artifacts import NewArtifact, WorkBriefIdentity
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.ports import WorkStore
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionResult
from pinboard.domain.identifiers import HostId, TaskId
from pinboard.mcp import common as mcp_common
from pinboard.mcp import server as mcp_server
from tests.artifact_support import write_revision
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject
from tests.work_brief_support import work_c_brief


class NativeLifecycleEffectsTest(CheckpointPackageSupport):
    def accept_execution_brief(
        self,
        fixture: CheckpointFixture,
        brief: work_brief_models.WorkBrief,
        *,
        legacy: bool,
    ) -> int:
        if legacy:
            payload = msgspec.to_builtins(brief)
            assert isinstance(payload, dict)
            payload["schema"] = "pinboard-work-brief/v2"
            del payload["checkout_selection"]
            del payload["obligation_correspondence"]
            content = msgspec.json.encode(payload, order="sorted") + b"\n"
        else:
            content = work_briefs.canonical_work_brief_bytes(brief)
        published = write_revision(
            resolve_durable_roots(fixture.project),
            NewArtifact(
                work_models.ArtifactKind.BRIEF,
                brief.attempt_id,
                brief.artifact_revision,
                ".json",
                content,
            ),
        )
        accepted = fixture.store.accept_artifact_reference(fixture.work, published, SQLITE_NOW)
        if isinstance(accepted, DecisionFailure):
            self.fail(str(accepted))
        return int(accepted.reference.artifact_ref_id)

    def test_activation_rebind_and_revised_resume_reject_legacy_and_checkout_mismatch_unchanged(self) -> None:
        for invalidity in ("legacy", "checkout-mismatch"):
            with self.subTest(route="activate", invalidity=invalidity):
                fixture = self.active_fixture()
                prepared = call_native_tool(
                    mcp_server.PREPARATION_AUTHORITY_TOOL,
                    {
                        "request": {
                            "project_root": str(fixture.project),
                            "work_root": str(fixture.work),
                            "operation": "start",
                            "item_id": "work-c",
                            "task_id": "preparer",
                            "host_id": "local",
                            "ttl_seconds": 300,
                        }
                    },
                )
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=fixture.project,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                base = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=fixture.project,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                definition_revision = prepared["definition_revision"]
                definition_digest = prepared["definition_digest"]
                assert isinstance(definition_revision, int)
                assert isinstance(definition_digest, str)
                brief = replace_struct(
                    work_c_brief(),
                    artifact_revision=2,
                    branch=branch,
                    base_revision=base,
                    owner_task_id="preparer",
                    accepted_scope=work_brief_models.AcceptedScope(definition_revision, definition_digest),
                    checkout_selection=work_models.CheckoutSelection.ISOLATED,
                )
                reference_id = self.accept_execution_brief(fixture, brief, legacy=invalidity == "legacy")
                action = self.native_actions(
                    fixture,
                    "activate",
                    "work-c",
                    role="preparer",
                    lease=self.json_object(prepared),
                )
                before = fixture.store.validated_snapshot()
                result = call_native_tool(
                    mcp_server.TRANSITION_TOOL,
                    {
                        "request": {
                            "project_root": str(fixture.project),
                            "work_root": str(fixture.work),
                            "role": "preparer",
                            "receipt": {
                                "action_id": action["action_id"],
                                "subject_revision": action["subject_revision"],
                            },
                            "payload": {"brief_artifact_ref_id": reference_id},
                            "lease_id": action["lease_id"],
                            "generation": action["generation"],
                        }
                    },
                )
                self.assertEqual("TRANSITION_INPUT_INVALID", result["code"], result)
                self.assertFalse(result["state_changed"])
                self.assertEqual(before, fixture.store.validated_snapshot())

            for route in ("rebind", "resume"):
                with self.subTest(route=route, invalidity=invalidity):
                    fixture = self.active_fixture()
                    brief = replace_struct(
                        fixture.brief,
                        artifact_revision=2,
                        checkout_selection=work_models.CheckoutSelection.ISOLATED,
                    )
                    reference_id = self.accept_execution_brief(fixture, brief, legacy=invalidity == "legacy")
                    if route == "rebind":
                        action = self.project_action(fixture, "rebind-attempt:work-a-1")
                        payload: JsonObject = {
                            "attempt": "work-a-1",
                            "branch": brief.branch,
                            "base_revision": brief.base_revision,
                            "brief_artifact_ref_id": reference_id,
                        }
                    else:
                        closed, stdout, stderr = self.run_cli(
                            *fixture.common,
                            "close",
                            "work-c",
                            "--outcome",
                            "done",
                            "--reason",
                            "Exercise revised-brief resume.",
                            "--task-id",
                            "review-owner",
                            "--host-id",
                            "local",
                        )
                        self.assertEqual(0, closed, f"{stdout}\n{stderr}")
                        pause = self.project_action(fixture, "pause:work-a-1")
                        self.assertEqual(
                            "committed",
                            self.transition_result(fixture, pause, {"reason": "Exercise revised-brief resume."})[
                                "status"
                            ],
                        )
                        actions = self.actions_result(fixture, {"role": "project"})
                        candidates = actions["actions"]
                        assert isinstance(candidates, list)
                        action = next(
                            self.json_object(value)
                            for value in candidates
                            if isinstance(value, dict) and self.json_object(value["action_id"])["kind"] == "resume"
                        )
                        payload = {"brief_artifact_ref_id": reference_id}
                    before = fixture.store.validated_snapshot()
                    result = self.transition_result(fixture, action, payload)
                    self.assertEqual("TRANSITION_INPUT_INVALID", result["code"], result)
                    self.assertFalse(result["state_changed"])
                    self.assertEqual(before, fixture.store.validated_snapshot())

    def test_retained_human_close_rejects_active_and_review_then_commits_exact_actor_with_warning(self) -> None:
        for fixture in (self.checkpoint_fixture(), self.active_fixture()):
            with self.subTest(state=fixture.store.validated_snapshot().lifecycle.work_items[0].state):
                before = fixture.store.validated_snapshot()
                arguments = (
                    *fixture.common,
                    "close",
                    "work-a",
                    "--outcome",
                    "done",
                    "--reason",
                    "Human decision.",
                    "--task-id",
                    "human-owner",
                    "--host-id",
                    "human-host",
                    "--json",
                )
                rejected, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual(11, rejected, f"{stdout}\\n{stderr}")
                failure = self.json_object(msgspec.json.decode(stdout))
                self.assertEqual("ACTION_NOT_AVAILABLE", failure["code"])
                self.assertFalse(failure["state_changed"])
                self.assertEqual([], failure["changed_surfaces"])
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        fixture = self.active_fixture()
        before = fixture.store.validated_snapshot()
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "controlled human-close view failure"),
        ):
            closed, stdout, stderr = self.run_cli(
                *fixture.common,
                "close",
                "work-c",
                "--outcome",
                "done",
                "--reason",
                "Human decision.",
                "--task-id",
                "human-owner",
                "--host-id",
                "human-host",
                "--json",
            )
        self.assertEqual(0, closed, f"{stdout}\\n{stderr}")
        result = self.json_object(msgspec.json.decode(stdout))
        committed = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, committed.lifecycle.project.revision)
        self.assertEqual(str(committed.lifecycle.project.revision), result["revision"])
        self.assertIn("SQLite transition succeeded", stderr)
        self.assertIn("controlled human-close view failure", stderr)
        receipt = next(
            value
            for value in reversed(committed.transition_receipts)
            if value.action_kind == decision_models.ActionKind.CLOSE
        )
        self.assertEqual((TaskId("human-owner"), HostId("human-host")), (receipt.actor_task_id, receipt.actor_host_id))
        outcome = self.json_object(msgspec.json.decode(receipt.outcome_payload))
        self.assertEqual(("done", "Human decision."), (outcome["outcome"], outcome["evidence"]))
        repaired, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, repaired, f"{stdout}\\n{stderr}")
        self.assertEqual(committed, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_paused_attempt_close_returns_review_recovery_without_an_escape_action(self) -> None:
        fixture = self.active_fixture()
        pause = self.project_action(fixture, "pause:work-a-1")
        paused = self.transition_result(fixture, pause, {"reason": "Preserve the accepted attempt."})
        self.assertEqual("committed", paused["status"])

        rejected, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-a",
            "--outcome",
            "dropped",
            "--reason",
            "Bypass review.",
            "--task-id",
            "human-owner",
            "--host-id",
            "human-host",
            "--json",
        )

        self.assertEqual(11, rejected, f"{stdout}\n{stderr}")
        failure = self.json_object(msgspec.json.decode(stdout))
        self.assertEqual("ACTION_NOT_AVAILABLE", failure["code"])
        actions = {self.json_object(value)["action_id"] for value in self.json_array(failure["next_actions"])}
        self.assertIn("rebind-attempt:work-a-1", actions)
        self.assertIn("revise-item:work-a", actions)
        self.assertNotIn("close:work-a", actions)

    def test_retained_human_close_rejects_selected_subject_change_under_shared_lock(self) -> None:
        fixture = self.active_fixture()
        competing = self.project_action(fixture, "defer:work-c")
        before = fixture.store.validated_snapshot()
        commit = lifecycle_operations.service.decide_and_commit_transition
        interleaved = False

        def commit_after_subject_change(
            store: WorkStore,
            command: decision_models.NonCheckpointTransitionCommand,
            decided_at: datetime,
            *,
            read_authorization_time: Callable[[], datetime],
            actor_task_id: TaskId | None,
            actor_host_id: HostId | None,
            transition_brief_identity: WorkBriefIdentity | None = None,
        ) -> DecisionResult[CommittedEffect]:
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                result = self.transition_result(
                    fixture, competing, {"timing": "safe-to-defer", "reopen_condition": "Resume after human review."}
                )
                self.assertEqual("committed", result["status"])
            return commit(
                store,
                command,
                decided_at,
                read_authorization_time=read_authorization_time,
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
                transition_brief_identity=transition_brief_identity,
            )

        with patch(
            "pinboard.adapters.lifecycle_operations.service.decide_and_commit_transition",
            side_effect=commit_after_subject_change,
        ):
            rejected, stdout, stderr = self.run_cli(
                *fixture.common,
                "close",
                "work-c",
                "--outcome",
                "done",
                "--reason",
                "Human decision.",
                "--task-id",
                "human-owner",
                "--host-id",
                "human-host",
                "--json",
            )
        self.assertEqual(11, rejected, f"{stdout}\\n{stderr}")
        failure = self.json_object(msgspec.json.decode(stdout))
        self.assertFalse(failure["state_changed"])
        self.assertEqual([], failure["changed_surfaces"])
        self.assertEqual("ACTION_NOT_AVAILABLE", failure["code"])
        self.assertEqual("do-not-retry", failure["retry"])
        current = self.project_action(fixture, "close:work-c")
        recovery = next(
            self.json_object(value)
            for value in self.json_array(failure["next_actions"])
            if self.json_object(value).get("action_id") == "close:work-c"
        )
        self.assertEqual(current["subject_revision"], recovery["subject_revision"])
        self.assertEqual("project", recovery["role"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        item = next(value for value in reloaded.lifecycle.work_items if str(value.item_id) == "work-c")
        self.assertEqual("deferred", item.state.value)
        self.assertFalse(
            any(
                value.action_kind == decision_models.ActionKind.CLOSE
                for value in reloaded.transition_receipts[len(before.transition_receipts) :]
            )
        )

    def test_revised_brief_resume_reloads_exact_scope_and_reference_atomically(self) -> None:
        fixture = self.active_fixture()
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The accepted prerequisite is satisfied.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")
        pause = self.project_action(fixture, "pause:work-a-1")
        self.assertEqual(
            "committed", self.transition_result(fixture, pause, {"reason": "Accept revised scope."})["status"]
        )
        current = call_native_tool(
            mcp_server.ITEM_DEFINITION_TOOL,
            {
                "request": {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "operation": "current",
                    "item_id": "work-a",
                }
            },
        )
        definition = self.json_object(current["definition"])
        revise = self.project_action(fixture, "revise-item:work-a")
        revised = self.transition_result(
            fixture,
            revise,
            {
                "schema": "pinboard-item-revision/v1",
                "item_id": "work-a",
                "expected_revision": current["definition_revision"],
                "expected_digest": current["definition_digest"],
                "source_task": "review-owner",
                "reason": "Clarify the supported current outcome.",
                "definition": {**definition, "objective": "Reload the new accepted scope and exact brief together."},
            },
        )
        self.assertEqual("committed", revised["status"])
        revision = next(
            value
            for value in reversed(fixture.store.validated_snapshot().lifecycle.definition_revisions)
            if str(value.item_id) == "work-a"
        )
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authorization = work_brief_models.AcceptedScopeAuthorization("work-a", revision.revision)
        brief = replace_struct(
            fixture.brief,
            artifact_revision=2,
            accepted_scope=work_brief_models.AcceptedScope(revision.revision, revision.digest),
            checkpoint=replace_struct(
                checkpoint,
                contracts=tuple(
                    replace_struct(contract, authorization_basis=authorization) for contract in checkpoint.contracts
                ),
                verification=tuple(
                    replace_struct(obligation, authorization_basis=authorization)
                    for obligation in checkpoint.verification
                ),
            ),
        )
        payload = self.json_object(msgspec.json.decode(msgspec.json.encode(brief)))
        published = call_native_tool(
            mcp_server.BRIEF_PUBLISH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "brief": payload},
        )
        self.assertEqual("committed", published["status"])
        reference = self.json_object(published["reference"])
        before = fixture.store.validated_snapshot()
        resume = self.project_action(fixture, "resume:work-a")
        result = self.transition_result(fixture, resume, {"brief_artifact_ref_id": reference["artifact_ref_id"]})
        self.assertEqual("committed", result["status"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        self.assertEqual(
            (work_models.AttemptState.ACTIVE, reference["artifact_ref_id"], revision.revision, revision.digest),
            (
                attempt.state,
                int(attempt.brief_artifact_ref_id),
                attempt.accepted_scope_revision,
                attempt.accepted_scope_digest,
            ),
        )
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(before.lifecycle.definition_revisions, reloaded.lifecycle.definition_revisions)

    def test_successive_native_replacement_proposals_reload_live_and_terminal_relation_revisions(self) -> None:
        for target in ("work-c", "work-b"):
            with self.subTest(target=target):
                fixture = self.active_fixture()
                before = fixture.store.validated_snapshot()
                for expected_revision in (1, 2):
                    proposal_id = f"replacement-{expected_revision}"
                    result = call_native_tool(
                        mcp_server.PROPOSAL_CREATE_TOOL,
                        {
                            "project_root": str(fixture.project),
                            "work_root": str(fixture.work),
                            "actor_task_id": "review-owner",
                            "actor_host_id": "local",
                            "proposal": {
                                "schema": "pinboard-proposal/v2",
                                "proposal_id": proposal_id,
                                "created_at": "2026-08-25T12:00:00+00:00",
                                "source_task_id": "review-owner",
                                "user_label": "Replace one known item",
                                "trigger": "A supported replacement exists.",
                                "evidence": ["source:current-contract"],
                                "why_it_matters": "Relation identity must advance.",
                                "relation": {
                                    "kind": "planned-replacement",
                                    "item": target,
                                    "replacement_cost": "The retained result would be replaced.",
                                },
                                "effect": "Record the exact proposed replacement.",
                                "unlock": "Review its disposition.",
                                "urgency_evidence": "The accepted consumer requires an exact relation revision.",
                                "freshness_assumptions": ["The affected item remains known."],
                                "checkout_policy": "coordinator-selected",
                                "obligations": [
                                    {
                                        "obligation_id": "review-disposition",
                                        "statement": "Review its disposition.",
                                        "deferral_policy": "forbidden",
                                    }
                                ],
                            },
                        },
                    )
                    self.assertEqual("committed", result["status"], result)
                    reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
                    relations = tuple(
                        value
                        for value in reloaded.replacements.planned_replacements
                        if str(value.affected_item_id) == target
                    )
                    self.assertEqual(
                        tuple(range(1, expected_revision + 1)), tuple(value.relation_revision for value in relations)
                    )
                    self.assertEqual(
                        tuple(f"replacement-{value}" for value in range(1, expected_revision + 1)),
                        tuple(str(value.replacement_item_id) for value in relations),
                    )
                    self.assertEqual(before.lifecycle.attempts, reloaded.lifecycle.attempts)
                    original = next(value for value in before.lifecycle.work_items if str(value.item_id) == target)
                    affected = next(value for value in reloaded.lifecycle.work_items if str(value.item_id) == target)
                    self.assertEqual(original.state, affected.state)

    def test_native_proposal_creation_refreshes_the_same_items_as_full_rebuild(self) -> None:
        for kind in ("independent", "prerequisite"):
            with self.subTest(kind=kind):
                fixture = self.active_fixture()
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
                item_ids = ("new-proposal",) if kind == "independent" else ("new-proposal", "work-c")
                if kind == "prerequisite":
                    (fixture.work / "views" / "items" / "work-c.md").write_bytes(b"stale projection\n")
                result = call_native_tool(
                    mcp_server.PROPOSAL_CREATE_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "actor_task_id": "review-owner",
                        "actor_host_id": "local",
                        "proposal": {
                            "schema": "pinboard-proposal/v2",
                            "proposal_id": "new-proposal",
                            "created_at": "2026-08-25T12:00:00+00:00",
                            "source_task_id": "review-owner",
                            "user_label": "One supported new proposal",
                            "trigger": "A supported relation exists.",
                            "evidence": ["source:current-contract"],
                            "why_it_matters": "Affected views must refresh.",
                            "relation": {"kind": kind, "item": None if kind == "independent" else "work-c"},
                            "effect": "Record the exact proposal.",
                            "unlock": "Inspect the current affected items.",
                            "urgency_evidence": "The accepted consumer requires immediate current views.",
                            "freshness_assumptions": ["The affected item remains known."],
                            "checkout_policy": "coordinator-selected",
                            "obligations": [
                                {
                                    "obligation_id": "inspect-affected-items",
                                    "statement": "Inspect the current affected items.",
                                    "deferral_policy": "forbidden",
                                }
                            ],
                        },
                    },
                )
                self.assertEqual("committed", result["status"], result)
                partial = {
                    item_id: (fixture.work / "views" / "items" / f"{item_id}.md").read_bytes() for item_id in item_ids
                }
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
                self.assertEqual(
                    partial,
                    {
                        item_id: (fixture.work / "views" / "items" / f"{item_id}.md").read_bytes()
                        for item_id in item_ids
                    },
                )

    def test_native_proposal_dispositions_refresh_the_same_item_bytes_as_full_rebuild(self) -> None:
        cases: tuple[tuple[str, JsonObject], ...] = (
            (
                "accept-proposal",
                {
                    "item": "zz-proposal-a",
                    "state": "ready",
                    "next_action": "activate",
                    "timing": None,
                    "depends_on": [],
                },
            ),
            ("merge-proposal", {"target": "work-c"}),
            ("return-proposal", {"reason": "Clarify the evidence."}),
            ("reject-proposal", {"reason": "The proposal is no longer needed."}),
        )
        for kind, payload in cases:
            with self.subTest(kind=kind):
                fixture = self.active_fixture()
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
                item_view = fixture.work / "views" / "items" / "zz-proposal-a.md"
                item_view.write_bytes(b"stale projection\n")
                action = self.project_action(fixture, f"{kind}:zz-proposal-a")
                result = self.transition_result(fixture, action, payload)
                self.assertEqual("committed", result["status"])
                partial = item_view.read_bytes()
                rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
                self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
                self.assertEqual(partial, item_view.read_bytes())

    def test_blocked_attempt_preserves_accepted_dependencies_and_resumes_after_human_closure(self) -> None:
        fixture = self.active_fixture()
        before = fixture.store.validated_snapshot()
        action = self.project_action(fixture, "block:work-a-1")
        result = self.transition_result(
            fixture, action, {"reason": "Wait for the accepted prerequisite.", "depends_on": ["work-c"]}
        )
        self.assertEqual("committed", result["status"])
        blocked = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        attempt = next(value for value in blocked.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        self.assertEqual(work_models.AttemptState.BLOCKED, attempt.state)
        self.assertEqual(before.lifecycle.dependencies, blocked.lifecycle.dependencies)
        unavailable = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "resume", "subject": "work-a"}}
        )
        self.assertEqual("ACTION_NOT_AVAILABLE", unavailable["code"])
        closed, stdout, stderr = self.run_cli(
            *fixture.common,
            "close",
            "work-c",
            "--outcome",
            "done",
            "--reason",
            "The accepted prerequisite is satisfied.",
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
        )
        self.assertEqual(0, closed, f"{stdout}\n{stderr}")
        resume = self.project_action(fixture, "resume:work-a")
        self.assertEqual("committed", self.transition_result(fixture, resume, {})["status"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        attempt = next(value for value in reloaded.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        prior_attempt = next(value for value in before.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        self.assertEqual(work_models.AttemptState.ACTIVE, attempt.state)
        self.assertEqual(
            (
                prior_attempt.brief_artifact_ref_id,
                prior_attempt.accepted_scope_revision,
                prior_attempt.accepted_scope_digest,
            ),
            (attempt.brief_artifact_ref_id, attempt.accepted_scope_revision, attempt.accepted_scope_digest),
        )

    def test_native_renewal_preserves_unchanged_generated_file_identity(self) -> None:
        fixture = self.active_fixture()
        common: JsonObject = {"project_root": str(fixture.project), "work_root": str(fixture.work)}
        lease = call_native_tool(
            mcp_server.ATTEMPT_AUTHORITY_TOOL,
            {
                "request": {
                    **common,
                    "operation": "acquire",
                    "attempt_id": "work-a-1",
                    "task_id": "current-worker",
                    "host_id": "local",
                    "ttl_seconds": 600,
                }
            },
        )
        self.assertEqual("committed", lease["status"])
        before = fixture.store.validated_snapshot()
        selectors = (
            "items/work-a.md",
            "attempts/work-a-1.md",
            f"history/{before.transition_receipts[-1].history_id}.md",
        )
        files = {
            selector: (
                (path := fixture.work / "views" / selector).read_bytes(),
                path.stat().st_ino,
                path.stat().st_mtime_ns,
            )
            for selector in selectors
        }
        renewed = call_native_tool(
            mcp_server.ATTEMPT_AUTHORITY_TOOL,
            {
                "request": {
                    **common,
                    "operation": "renew",
                    "attempt_id": "work-a-1",
                    "lease_id": lease["lease_id"],
                    "generation": lease["generation"],
                    "ttl_seconds": 1200,
                }
            },
        )
        self.assertEqual("committed", renewed["status"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(
            files,
            {
                selector: (
                    (path := fixture.work / "views" / selector).read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for selector in selectors
            },
        )
        newest = fixture.work / "views" / "history" / f"{reloaded.transition_receipts[-1].history_id}.md"
        self.assertTrue(newest.is_file())
        rebuilt, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, rebuilt, f"{stdout}\n{stderr}")
        self.assertEqual(
            files,
            {
                selector: (
                    (path := fixture.work / "views" / selector).read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for selector in selectors
            },
        )

    def active_fixture(self) -> CheckpointFixture:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "return-for-correction:work-a-1")
        result = self.transition_result(fixture, action, {"reason": "Continue the current native attempt."})
        self.assertEqual("committed", result["status"])
        return fixture

    def test_direct_transition_preserves_its_receipt_before_or_after_disjoint_commit(self) -> None:
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                self.assert_interleaved_receipt(stage)

    def assert_interleaved_receipt(self, stage: str) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        competing = self.project_action(fixture, "defer:work-c")
        before = fixture.store.validated_snapshot()
        commit = lifecycle_operations.service.decide_and_commit_transition
        interleaved = False

        def commit_with_competitor(
            store: WorkStore,
            command: decision_models.NonCheckpointTransitionCommand,
            decided_at: datetime,
            *,
            read_authorization_time: Callable[[], datetime],
            actor_task_id: TaskId | None,
            actor_host_id: HostId | None,
            transition_brief_identity: WorkBriefIdentity | None = None,
        ) -> DecisionResult[CommittedEffect]:
            nonlocal interleaved
            owns_competitor = not interleaved
            interleaved = True
            if owns_competitor and stage == "before":
                result = self.transition_result(
                    fixture,
                    competing,
                    {"timing": "safe-to-defer", "reopen_condition": "Resume after current evidence."},
                )
                self.assertEqual("committed", result["status"])
            committed = commit(
                store,
                command,
                decided_at,
                read_authorization_time=read_authorization_time,
                actor_task_id=actor_task_id,
                actor_host_id=actor_host_id,
                transition_brief_identity=transition_brief_identity,
            )
            if owns_competitor and stage == "after":
                result = self.transition_result(
                    fixture,
                    competing,
                    {"timing": "safe-to-defer", "reopen_condition": "Resume after current evidence."},
                )
                self.assertEqual("committed", result["status"])
            return committed

        with patch.object(lifecycle_operations.service, "decide_and_commit_transition", commit_with_competitor):
            result = self.transition_result(fixture, action, {"reason": "Pause at a stable checkpoint."})
        self.assertEqual("committed", result["status"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        own_receipt = next(
            value
            for value in reversed(reloaded.transition_receipts)
            if value.action_kind == decision_models.ActionKind.PAUSE
        )
        self.assertEqual(own_receipt.project_revision, result["committed_revision"])
        self.assertEqual(int(own_receipt.history_id), result["history_id"])
        self.assertEqual(before.lifecycle.project.revision + 2, reloaded.lifecycle.project.revision)
        work_c = next(value for value in reloaded.lifecycle.work_items if str(value.item_id) == "work-c")
        self.assertEqual("deferred", work_c.state.value)

    def test_post_commit_view_failure_reports_warning_and_repairs_without_replay(self) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        before = fixture.store.validated_snapshot()
        with patch(
            "pinboard.adapters.files.views.atomic_replace",
            side_effect=FileIOError(FileIOErrorCode.FILE_PUBLISH_FAILED, "controlled view failure"),
        ):
            result = self.transition_result(fixture, action, {"reason": "Pause before repairing views."})
        self.assertEqual("committed-with-warning", result["status"])
        warning = self.json_object(result["warning"])
        self.assertIn("pinboard views rebuild", str(warning["recovery"]))
        committed = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, committed.lifecycle.project.revision)
        repaired, stdout, stderr = self.run_cli(*fixture.common, "views", "rebuild")
        self.assertEqual(0, repaired, f"{stdout}\n{stderr}")
        self.assertEqual(committed, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_unexpected_projection_exception_preserves_committed_transition_for_fresh_read(self) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        before = fixture.store.validated_snapshot()
        error = RuntimeError("controlled unexpected projection exception")
        with (
            patch.object(mcp_common, "_refresh_affected_views", side_effect=error),
            self.assertRaises(UnexpectedToolError) as failure,
        ):
            self.transition_result(fixture, action, {"reason": "Pause before exceptional reply loss."})
        self.assertIs(error, failure.exception.__cause__)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        attempt = next(value for value in reloaded.lifecycle.attempts if str(value.attempt_id) == "work-a-1")
        self.assertEqual(work_models.AttemptState.PAUSED, attempt.state)
        self.assertEqual(decision_models.ActionKind.PAUSE, reloaded.transition_receipts[-1].action_kind)

    def test_follow_up_inspection_cannot_load_complete_state_after_view_refresh(self) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        original_refresh = mcp_common._refresh_affected_views
        with patch.object(mcp_common, "_refresh_affected_views", wraps=original_refresh) as refreshed:
            result = self.transition_result(fixture, action, {"reason": "Pause before bounded inspection."})
        self.assertEqual("committed", result["status"])
        refreshed.assert_called_once()
        with (
            patch.object(SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("Complete snapshot used")),
            patch.object(sqlite_state, "read_state", side_effect=AssertionError("Complete state assembly used")),
        ):
            inspected = call_native_tool(
                mcp_server.ATTEMPT_INSPECT_TOOL,
                {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "attempt_id": "work-a-1",
                    "reconciliation": None,
                },
            )
        continuation = self.json_object(inspected["continuation"])
        self.assertEqual("paused", continuation["state"])

    def test_dependency_corruption_rejects_native_discovery_and_transition_without_writes(self) -> None:
        fixture = self.active_fixture()
        action = self.project_action(fixture, "pause:work-a-1")
        database = fixture.work / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DELETE FROM item_dependencies WHERE item_id = 'work-a'")
            connection.commit()
            before = tuple(connection.iterdump())
        queries: tuple[JsonObject, ...] = (
            {"role": "project"},
            {"role": "project", "action_id": {"kind": "pause", "subject": "work-a-1"}},
        )
        for query in queries:
            with self.subTest(query=query), self.assertRaises(UnexpectedToolError) as failure:
                self.actions_result(fixture, query)
            self.assertIsInstance(failure.exception.__cause__, StorageError)
            error = failure.exception.__cause__
            assert isinstance(error, StorageError)
            self.assertEqual(StorageErrorCode.INVALID_STATE, error.code)
        with self.assertRaises(UnexpectedToolError) as failure:
            self.transition_result(fixture, action, {"reason": "Pause before corrupt dependency writes."})
        self.assertIsInstance(failure.exception.__cause__, StorageError)
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            self.assertEqual(before, tuple(connection.iterdump()))
