import base64
import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, tzinfo
from itertools import count
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import msgspec

from pinboard.adapters import dispatch_operations
from pinboard.adapters.files import root
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    action_models,
    artifact_identity,
    candidate_snapshots,
    query_models,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application import (
    dispatch as application_dispatch,
)
from pinboard.application.artifact_publication import ArtifactAcceptanceFailure, ArtifactWriteFailure
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.application.dispatch_models import DispatchArtifactPort
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, CheckpointId, HistoryId, ReviewId
from pinboard.mcp import contract_schemas, server
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import job_operations as mcp_jobs
from pinboard.mcp.contracts import JsonValue
from tests import test_dispatch
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import native_tools
from tests.support import SQLITE_NOW, JsonObject
from tests.work_brief_support import ready_review, work_a_brief


class CorrectionSourceReviewTest(CheckpointPackageSupport):
    def native_fixture(self, fixture: CheckpointFixture) -> CheckpointFixture:
        native = self.enterContext(native_tools())
        return dataclasses.replace(
            fixture,
            call_native_tool=native.call_native_tool,
            call_advertised_tool=native.call_advertised_tool,
        )

    def correction_fixture(
        self, *, review_condition: Literal["ready", "missing", "malformed", "stale", "wrong-owner"] = "ready"
    ) -> CheckpointFixture:
        fixture = self.native_fixture(self.checkpoint_fixture(review_condition=review_condition))
        payload = fixture.work / "bootstrap-return.json"
        payload.write_text('{"reason":"Prepare the real correction scenario."}', encoding="utf-8")
        self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        tests = fixture.project / "tests"
        tests.mkdir()
        (tests / "test_only.py").write_text("assert True\n", encoding="utf-8")
        self.commit_all(fixture.project, "test baseline")
        return fixture

    def submit_and_return(
        self,
        fixture: CheckpointFixture,
        label: str,
        *,
        committed: bool,
        reason: str = "Repair the test-only candidate.",
    ) -> tuple[int, JsonObject]:
        (fixture.project / "tests" / "test_only.py").write_text(f"assert {label!r}\n", encoding="utf-8")
        candidate = (
            self.commit_all(fixture.project, label)
            if committed
            else root.read_working_tree_candidate(fixture.project).identity
        )
        lease = self.native_attempt_acquire(fixture, label)
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        payload = fixture.work / "submit.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        self.transition_json(fixture, action, payload)
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        reference = context.reference
        payload.write_text(json.dumps({"reason": reason}), encoding="utf-8")
        receipt = self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
        history_id = receipt["history_id"]
        assert isinstance(history_id, int)
        return history_id, self.correction_choice(fixture, label, reference, history_id, reason)

    def correction_choice(
        self,
        fixture: CheckpointFixture,
        label: str,
        reference: stored_state.ArtifactReference,
        history_id: int,
        reason: str,
    ) -> JsonObject:
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        authorities: list[work_brief_models.ReviewedAuthority] = []
        for authority in checkpoint.reviewed_authorities:
            selected = select_checkout_brief_source(fixture.project, authority_selector(authority.selector), True)
            assert not isinstance(selected, BriefSourceFailure)
            authorities.append(
                msgspec.structs.replace(authority, reviewed_sha256=hashlib.sha256(selected.content).hexdigest())
            )
        effective_brief = msgspec.structs.replace(
            fixture.brief, checkpoint=msgspec.structs.replace(checkpoint, reviewed_authorities=tuple(authorities))
        )
        review = work_brief_models.CorrectionSourceReview(
            "pinboard-correction-source-review/v1",
            msgspec.json.decode(ready_review(effective_brief), type=work_brief_models.WorkBriefReview),
            work_brief_models.PortableArtifactIdentity(
                "candidate",
                "evidence",
                reference.key,
                reference.revision,
                reference.selector,
                reference.content_sha256,
                reference.size_bytes,
            ),
            action_models.ReasonInputPayload(reason),
            "The complete accepted starting snapshot includes the changed test; the proposed repair preserves the reviewed contracts.",
        )
        action = self.project_action(fixture, "dispatch:work-a-1")
        environment = msgspec.structs.replace(
            test_dispatch.DispatchTest().environment(fixture.project),
            branch=fixture.brief.branch,
            starting_revision=fixture.brief.base_revision,
        )
        choice = msgspec.to_builtins(
            {
                "kind": "correction",
                "receipt": {
                    "action_id": {"kind": "dispatch", "subject": "work-a-1"},
                    "subject_revision": action["subject_revision"],
                },
                "checkpoint_id": fixture.brief.checkpoint.checkpoint_id,
                "environment": environment,
                "prompt": None,
                "brief_review": review,
                "review_id": label,
                "correction_history_id": history_id,
            },
            enc_hook=test_dispatch.dispatch_environment_enc_hook,
        )
        assert isinstance(choice, dict)
        return choice

    def dispatch_native(self, fixture: CheckpointFixture, choice: JsonObject) -> JsonObject:
        return mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
        ).content

    def reuse_choice(self, fixture: CheckpointFixture, choice: JsonObject) -> JsonObject:
        selected = deepcopy(choice)
        original = self.json_object(selected["brief_review"])
        contract = self.json_object(original["contract_review"])
        selected["kind"] = "reuse-correction"
        selected["brief_review"] = {
            "schema": "pinboard-correction-source-review/v2",
            "accepted_brief_sha256": hashlib.sha256(work_briefs.canonical_work_brief_bytes(fixture.brief)).hexdigest(),
            "reviewer_task_id": contract["reviewer_task_id"],
            "starting_candidate": original["starting_candidate"],
            "correction_input": original["correction_input"],
            "assessment": original["assessment"],
        }
        return selected

    def fixed_native_time(self) -> None:
        for module in (
            "pinboard.mcp.mutation_operations",
            "pinboard.mcp.read_operations",
            "pinboard.mcp.job_operations",
            "tests.checkpoint_support",
        ):
            clock = self.enterContext(patch(f"{module}.datetime", wraps=datetime))
            clock.now.return_value = SQLITE_NOW

    def pause_and_resume(self, fixture: CheckpointFixture, rounds: int) -> CheckpointFixture:
        action = self.project_action(fixture, "close:work-c")
        closed = fixture.call_native_tool(
            server.CLOSE_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "receipt": {"action_id": action["action_id"], "subject_revision": action["subject_revision"]},
                "payload": {
                    "outcome": "done",
                    "reason": "The prerequisite is satisfied.",
                    "human_decision": "Close the satisfied prerequisite.",
                },
                "actor_task_id": "review-owner",
                "actor_host_id": "local",
            },
        )
        self.assertEqual("committed", closed["status"], closed)
        for _ in range(rounds):
            paused = self.transition_result(
                fixture, self.project_action(fixture, "pause:work-a-1"), {"reason": "Wait for the maintainer."}
            )
            self.assertEqual("committed", paused["status"], paused)
            resumed = self.transition_result(fixture, self.project_action(fixture, "resume:work-a"), {})
            self.assertEqual("committed", resumed["status"], resumed)
        return dataclasses.replace(fixture, store=SQLiteWorkStore(fixture.work / "state.sqlite3"))

    def test_native_correction_survives_same_brief_pause_resume_with_fresh_dispatch_receipt(self) -> None:
        self.fixed_native_time()
        for rounds, reuse in ((1, False), (3, False), (1, True), (3, True)):
            with self.subTest(rounds=rounds, reuse=reuse):
                fixture = self.correction_fixture()
                history_id, choice = self.submit_and_return(fixture, "paused-test", committed=True)
                if reuse:
                    choice = self.reuse_choice(fixture, choice)
                fixture = self.pause_and_resume(fixture, rounds)
                before = fixture.store.validated_snapshot()
                context = fixture.call_native_tool(
                    server.CORRECTION_CONTEXT_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "attempt_id": "work-a-1",
                        "correction_history_id": history_id,
                    },
                )
                self.assertEqual("ready", context["status"], context)
                self.assertEqual(history_id, context["correction_history_id"])
                self.assertEqual("Repair the test-only candidate.", context["correction_reason"])
                self.assertEqual(before, fixture.store.validated_snapshot())
                stale = self.dispatch_native(fixture, choice)
                self.assertEqual("STALE_ACTION", stale["code"], stale)
                self.assertEqual(before, fixture.store.validated_snapshot())
                action = self.project_action(fixture, "dispatch:work-a-1")
                choice["receipt"] = {
                    "action_id": action["action_id"],
                    "subject_revision": action["subject_revision"],
                }
                dispatched = fixture.call_native_tool(
                    server.DISPATCH_TOOL,
                    {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": choice},
                )
                self.assertEqual("ready", dispatched["status"], dispatched)

    def test_multiline_return_reason_reaches_correction_context_and_dispatch_unchanged(self) -> None:
        self.fixed_native_time()
        reason = (
            "The test-only candidate drops one assertion.\n\n"
            "Problem:\n- `tests/test_only.py` asserts a label.\nFix:\n1. Restore the assertion.\n\n"
            "References: review.md finding 1."
        )
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                fixture = self.correction_fixture()
                history_id, choice = self.submit_and_return(fixture, "multiline", committed=True, reason=reason)
                if reuse:
                    choice = self.reuse_choice(fixture, choice)
                context = fixture.call_advertised_tool(
                    server.CORRECTION_CONTEXT_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "attempt_id": "work-a-1",
                        "correction_history_id": history_id,
                    },
                )
                self.assertEqual(reason, context["correction_reason"])
                dispatched = fixture.call_advertised_tool(
                    server.DISPATCH_TOOL,
                    {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": choice},
                )
                self.assertEqual("ready", dispatched["status"], dispatched)
                prompt_reference = self.json_object(dispatched["prompt_reference"])
                prompt = (fixture.work / str(prompt_reference["selector"])).read_text(encoding="utf-8")
                self.assertIn(
                    f"- Canonical return reason (JSON string): {msgspec.json.encode(reason).decode()}\n", prompt
                )

    def test_accepted_continuation_supersedes_paused_correction_return(self) -> None:
        self.fixed_native_time()
        fixture = self.correction_fixture()
        history_id, choice = self.submit_and_return(fixture, "returned-test", committed=True)
        fixture = self.pause_and_resume(fixture, 1)
        (fixture.project / "tests" / "test_only.py").write_text("assert 'corrected'\n", encoding="utf-8")
        candidate = self.commit_all(fixture.project, "corrected candidate")
        lease = self.native_attempt_acquire(fixture, "correction-worker")
        submitted = self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
            {"candidate": candidate},
        )
        self.assertEqual("committed", submitted["status"], submitted)
        self.record_commissioned_review(fixture, candidate, "independent-reviewer")
        continued = self.transition_result(
            fixture,
            self.project_action(fixture, "accept-review-and-continue:work-a-1"),
            {"candidate": candidate, "evidence": "Accepted; continue the attempt."},
        )
        self.assertEqual("committed", continued["status"], continued)
        fixture = dataclasses.replace(fixture, store=SQLiteWorkStore(fixture.work / "state.sqlite3"))
        before = fixture.store.validated_snapshot()
        context = fixture.call_native_tool(
            server.CORRECTION_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "correction_history_id": history_id,
            },
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", context["code"], context)
        action = self.project_action(fixture, "dispatch:work-a-1")
        choice["receipt"] = {"action_id": action["action_id"], "subject_revision": action["subject_revision"]}
        dispatched = self.dispatch_native(fixture, choice)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", dispatched["code"], dispatched)
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_reuse_correction_requires_fresh_independent_candidate_assessment(self) -> None:
        fixture = self.correction_fixture()
        history_id, full = self.submit_and_return(fixture, "reuse-review", committed=True)
        choice = self.reuse_choice(fixture, full)
        context = fixture.call_native_tool(
            server.CORRECTION_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "correction_history_id": history_id,
            },
        )
        self.assertTrue(context["reuse_eligible"])
        self.assertEqual([], context["reuse_blockers"])
        before = fixture.store.validated_snapshot()
        changes: tuple[tuple[str, work_brief_models.WorkBriefJsonValue, str], ...] = (
            ("reviewer_task_id", fixture.brief.owner_task_id, "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT"),
            ("accepted_brief_sha256", "f" * 64, "DISPATCH_BRIEF_REVIEW_STALE"),
            ("correction_input", {"reason": "different"}, "DISPATCH_BRIEF_REVIEW_STALE"),
        )
        for field, value, code in changes:
            invalid = deepcopy(choice)
            self.json_object(invalid["brief_review"])[field] = value
            self.assertEqual(code, self.dispatch_native(fixture, invalid)["code"])
            self.assertEqual(before, fixture.store.validated_snapshot())
        invalid = deepcopy(choice)
        self.json_object(self.json_object(invalid["brief_review"])["starting_candidate"])["content_sha256"] = "f" * 64
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", self.dispatch_native(fixture, invalid)["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        malformed = deepcopy(choice)
        self.json_object(malformed["brief_review"])["unknown"] = True
        self.assertEqual("DISPATCH_INVALID", self.dispatch_native(fixture, malformed)["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        ready = fixture.call_native_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": choice},
        )
        self.assertEqual("ready", ready["status"])
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        persisted = [
            (fixture.work / reference.selector).read_bytes()
            for reference in fresh.artifact_references
            if reference.kind == work_models.ArtifactKind.EVIDENCE
            and b"pinboard-correction-source-review/v2" in (fixture.work / reference.selector).read_bytes()
        ]
        self.assertEqual(1, len(persisted))
        decoded = msgspec.json.decode(persisted[0], type=work_brief_models.ReusedCoverageCorrectionReview)
        self.assertEqual(persisted[0], work_briefs.canonical_correction_source_review_bytes(decoded))

    def test_reuse_correction_reports_changed_source_and_missing_review(self) -> None:
        review_conditions: tuple[Literal["ready", "missing", "malformed", "stale", "wrong-owner"], ...] = (
            "ready",
            "missing",
            "malformed",
            "stale",
            "wrong-owner",
        )
        for review_condition in review_conditions:
            with self.subTest(review_condition=review_condition):
                fixture = self.correction_fixture(review_condition=review_condition)
                if review_condition == "ready":
                    (fixture.project / "architecture.md").write_text(
                        "# Architecture\n\n## Contract\n\nChanged after accepted review.\n", encoding="utf-8"
                    )
                history_id, full = self.submit_and_return(fixture, review_condition, committed=True)
                choice = self.reuse_choice(fixture, full)
                before = fixture.store.validated_snapshot()
                context = fixture.call_native_tool(
                    server.CORRECTION_CONTEXT_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "attempt_id": "work-a-1",
                        "correction_history_id": history_id,
                    },
                )
                self.assertFalse(context["reuse_eligible"])
                self.assertTrue(context["reuse_blockers"])
                self.assertIn("full cross-boundary correction review", str(context["reuse_blockers"]))
                rejected = self.dispatch_native(fixture, choice)
                self.assertIn(
                    rejected["code"],
                    (
                        "DISPATCH_BRIEF_REVIEW_STALE",
                        "DISPATCH_BRIEF_REVIEW_MISSING",
                        "DISPATCH_BRIEF_REVIEW_INVALID",
                        "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT",
                    ),
                )
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_reuse_correction_reports_missing_reviewed_source(self) -> None:
        fixture = self.correction_fixture()
        history_id, full = self.submit_and_return(fixture, "missing-source", committed=True)
        (fixture.project / "architecture.md").unlink()
        choice = self.reuse_choice(fixture, full)
        before = fixture.store.validated_snapshot()
        context = fixture.call_native_tool(
            server.CORRECTION_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "correction_history_id": history_id,
            },
        )
        self.assertEqual("DISPATCH_AUTHORITY_UNREADABLE", context["code"])
        self.assertIn("Restore that source", str(context["message"]))
        self.assertEqual("DISPATCH_AUTHORITY_UNREADABLE", self.dispatch_native(fixture, choice)["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_local_return_dispatches_and_corrected_commit_reaches_disposition(self) -> None:  # noqa: PLR0915 - one native recovery journey
        fixture = self.native_fixture(self.checkpoint_fixture(local=True))
        context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        reason = "Submit a clean commit for repository disposition."
        history_id = self.return_for_correction(fixture, reason, "local")
        fixture = self.pause_and_resume(fixture, 2)
        reference = context.reference
        review = work_brief_models.LocalCorrectionSourceReview(
            "pinboard-local-correction-source-review/v1",
            hashlib.sha256(work_briefs.canonical_work_brief_bytes(fixture.brief)).hexdigest(),
            "independent-local-reviewer",
            work_brief_models.PortableArtifactIdentity(
                "candidate",
                "evidence",
                reference.key,
                reference.revision,
                reference.selector,
                reference.content_sha256,
                reference.size_bytes,
            ),
            action_models.ReasonInputPayload(reason),
            "The accepted working-tree snapshot is complete and the clean commit preserves local scope.",
        )
        action = self.project_action(fixture, "dispatch:work-a-1")
        environment = msgspec.structs.replace(
            test_dispatch.DispatchTest().environment(fixture.project),
            branch=fixture.brief.branch,
            starting_revision=fixture.brief.base_revision,
        )
        choice = msgspec.to_builtins(
            {
                "kind": "local-correction",
                "receipt": {
                    "action_id": {"kind": "dispatch", "subject": "work-a-1"},
                    "subject_revision": action["subject_revision"],
                },
                "checkpoint_id": fixture.brief.checkpoint.checkpoint_id,
                "environment": environment,
                "prompt": None,
                "brief_review": review,
                "review_id": "local-correction-review",
                "correction_history_id": history_id,
            },
            enc_hook=test_dispatch.dispatch_environment_enc_hook,
        )
        assert isinstance(choice, dict)
        before = fixture.store.validated_snapshot()
        for changed, code in (
            ({"accepted_brief_sha256": "f" * 64}, "DISPATCH_BRIEF_REVIEW_STALE"),
            ({"reviewer_task_id": fixture.brief.owner_task_id}, "DISPATCH_BRIEF_REVIEW_NOT_INDEPENDENT"),
        ):
            invalid = deepcopy(choice)
            self.json_object(invalid["brief_review"]).update(changed)
            rejected = self.dispatch_native(fixture, invalid)
            self.assertEqual(code, rejected["code"], rejected)
            self.assertEqual("unchanged", rejected["effect"])
            self.assertEqual(before, fixture.store.validated_snapshot())
        wrong_shape = self.dispatch_native(fixture, choice | {"kind": "correction"})
        self.assertEqual("DISPATCH_INVALID", wrong_shape["code"])
        wrong_snapshot = deepcopy(choice)
        self.json_object(self.json_object(wrong_snapshot["brief_review"])["starting_candidate"])["content_sha256"] = (
            "f" * 64
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", self.dispatch_native(fixture, wrong_snapshot)["code"])
        stale_return = self.dispatch_native(fixture, choice | {"correction_history_id": history_id + 999})
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", stale_return["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        (fixture.project / "tracked.txt").write_text("drift\n", encoding="utf-8")
        drifted = self.dispatch_native(fixture, choice)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", drifted["code"])
        self.assertEqual("unchanged", drifted["effect"])
        (fixture.project / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        dispatched = fixture.call_native_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": choice},
        )
        self.assertEqual("ready", dispatched["status"], dispatched)

        (fixture.project / "tracked.txt").write_text("independently assessed local drift\n", encoding="utf-8")
        current_context = fixture.call_advertised_tool(
            server.RETURN_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "selection": {"kind": "current-candidate", "return_history_id": history_id},
            },
        )
        self.assertEqual("ready", current_context["status"], current_context)
        current_choice = deepcopy(choice)
        current_choice["kind"] = "candidate-correction"
        current_choice["review_id"] = "local-drift-review"
        current_choice["brief_review"] = {
            "schema": "pinboard-current-correction-source-review/v1",
            "boundary": "local",
            "source_review": choice["brief_review"],
            "current_candidate": self.json_object(current_context["context"])["current_candidate"],
        }
        current_result = fixture.call_advertised_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current_choice},
        )
        self.assertEqual("ready", current_result["status"], current_result)

        (fixture.project / "tracked.txt").write_text("corrected\n", encoding="utf-8")
        candidate = self.commit_all(fixture.project, "corrected local candidate")
        lease = self.native_attempt_acquire(fixture, "local-correction-worker")
        submit = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        payload = fixture.work / "submit-local.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        self.transition_json(fixture, submit, payload)
        review_job = fixture.call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": {
                    "kind": "correction",
                    "attempt_id": "work-a-1",
                    "candidate_revision": candidate,
                    "correction_history_id": history_id,
                    "runtime": "codex",
                    "background": False,
                },
            },
        )
        self.assertEqual("ready", review_job["status"], review_job)
        snapshot = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        attempt = fixture.store.read_attempt_context(AttemptId("work-a-1"))
        assert snapshot is not None and isinstance(attempt, query_models.NonterminalAttemptContextFacts)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        recorded = fixture.call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": {
                    "kind": "record-ready",
                    "attempt_id": "work-a-1",
                    "candidate_revision": candidate,
                    "candidate_snapshot_sha256": snapshot.reference.content_sha256,
                    "accepted_brief_sha256": attempt.brief_reference.content_sha256,
                    "result_sha256": hashlib.sha256((attempt_root / "result.md").read_bytes()).hexdigest(),
                    "review_sha256": hashlib.sha256((attempt_root / "review.md").read_bytes()).hexdigest(),
                    "reviewer_prompt_sha256": self.json_object(review_job["prompt_reference"])["sha256"],
                    "reviewer_task_id": "independent-candidate-reviewer",
                    "verdict": "ready",
                    "acceptance_evidence": "The corrected commit satisfies the same accepted local brief.",
                },
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        inspected = fixture.call_native_tool(
            server.ATTEMPT_INSPECT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "reconciliation": {
                    "target_revision": "accepted-base",
                    "relation": "candidate-pending-on-accepted-base",
                    "phase": "disposition",
                    "effects": [
                        {"effect": name, "status": "allowed"}
                        for name in ("source-checkout", "shared-work-root", "git-metadata")
                    ],
                },
            },
        )
        operation = self.json_object(self.json_object(inspected["continuation"])["next_operation"])
        self.assertEqual("repository-disposition", operation["kind"], inspected)

    def test_cross_base_replacement_reviews_rebased_candidate_and_preserves_history(self) -> None:
        self.fixed_native_time()
        for candidate_form in ("commit", "working-tree"):
            with self.subTest(candidate_form=candidate_form):
                self.cross_base_replacement_recovery(candidate_form)

    def cross_base_replacement_recovery(self, candidate_form: str) -> None:  # noqa: PLR0915 - one native persisted cross-base journey
        fixture = self.correction_fixture()
        old_base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        (fixture.project / "tests" / "test_only.py").write_text("assert 'original'\n", encoding="utf-8")
        original = self.commit_all(fixture.project, "original candidate")
        lease = self.native_attempt_acquire(fixture, "original-worker")
        submitted = self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
            {"candidate": original},
        )
        self.assertEqual("committed", submitted["status"], submitted)
        original_review = self.record_commissioned_review(fixture, original, "original-reviewer")
        original_review_reference = self.json_object(original_review["candidate_review"])
        original_review_path = fixture.work / str(original_review_reference["selector"])
        original_review_bytes = original_review_path.read_bytes()
        original_verdict = self.json_object(json.loads(original_review_bytes))
        original_context = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert original_context is not None
        original_snapshot = (fixture.work / original_context.reference.selector).read_bytes()
        returned = self.transition_result(
            fixture,
            self.project_action(fixture, "return-for-correction:work-a-1"),
            {"reason": "Reassess the candidate on its new accepted base."},
        )
        history_id = returned["history_id"]
        assert isinstance(history_id, int)
        subprocess.run(
            ["git", "switch", "-c", "new-base", old_base], cwd=fixture.project, check=True, capture_output=True
        )
        (fixture.project / "tracked.txt").write_text("new base\n", encoding="utf-8")
        new_base = self.commit_all(fixture.project, "new accepted base")
        subprocess.run(["git", "switch", fixture.brief.branch], cwd=fixture.project, check=True, capture_output=True)
        rebase_environment = os.environ.copy()
        for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            rebase_environment.pop(variable, None)
        rebase_environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        rebase = subprocess.run(
            [
                "git",
                "-c",
                "user.useConfigOnly=true",
                "-c",
                "user.name=Pinboard Tests",
                "-c",
                "user.email=pinboard@example.invalid",
                "rebase",
                "--onto",
                new_base,
                old_base,
            ],
            cwd=fixture.project,
            env=rebase_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, rebase.returncode, rebase.stderr)
        rebased = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertNotEqual(original, rebased)
        self.assertNotEqual(fixture.brief.base_revision, new_base)
        if candidate_form == "working-tree":
            (fixture.project / "tests" / "test_only.py").write_text("assert 'rebased working tree'\n", encoding="utf-8")
            rebased = root.read_working_tree_candidate(fixture.project).identity
        replacement = msgspec.structs.replace(
            fixture.brief,
            artifact_revision=fixture.brief.artifact_revision + 1,
            base_revision=new_base,
        )
        before = fixture.store.validated_snapshot()
        wrong_scope = msgspec.to_builtins(replacement)
        assert isinstance(wrong_scope, dict)
        self.json_object(wrong_scope["accepted_scope"])["revision"] = 2
        rejected = fixture.call_native_tool(
            server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": wrong_scope,
            },
        )
        self.assertEqual("rejected", rejected["status"], rejected)
        self.assertEqual(before, fixture.store.validated_snapshot())
        published = fixture.call_native_tool(
            server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": msgspec.to_builtins(replacement),
            },
        )
        self.assertEqual("committed", published["status"], published)
        brief_reference = self.json_object(published["reference"])
        before = fixture.store.validated_snapshot()
        rejected = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": replacement.branch,
                "base_revision": fixture.brief.base_revision,
                "brief_artifact_ref_id": brief_reference["artifact_ref_id"],
            },
        )
        self.assertEqual("rejected", rejected["status"], rejected)
        self.assertEqual(before, fixture.store.validated_snapshot())
        rebound = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": replacement.branch,
                "base_revision": new_base,
                "brief_artifact_ref_id": brief_reference["artifact_ref_id"],
            },
        )
        self.assertEqual("committed", rebound["status"], rebound)
        fixture = dataclasses.replace(fixture, brief=replacement, store=SQLiteWorkStore(fixture.work / "state.sqlite3"))
        choice = self.correction_choice(
            fixture,
            "cross-base-readiness",
            original_context.reference,
            history_id,
            "Reassess the candidate on its new accepted base.",
        )
        historical = fixture.call_advertised_tool(
            server.RETURN_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "selection": {"kind": "historical", "return_history_id": history_id},
            },
        )
        self.assertEqual("ready", historical["status"], historical)
        recovered = self.json_object(historical["context"])
        self.assertEqual(original, self.json_object(recovered["starting_snapshot"])["candidate"])
        self.assertEqual(history_id, self.json_object(recovered["return_receipt"])["history_id"])
        self.assertEqual(
            original_context.reference.content_sha256,
            self.json_object(recovered["starting_candidate"])["content_sha256"],
        )
        self.json_object(choice["brief_review"])["starting_candidate"] = recovered["starting_candidate"]
        self.json_object(choice["brief_review"])["correction_input"] = {"reason": recovered["correction_reason"]}
        forbidden = fixture.call_advertised_tool(
            server.RETURN_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "selection": {"kind": "current-candidate", "return_history_id": history_id},
            },
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", forbidden["code"], forbidden)
        choice["kind"] = "replacement-readiness"
        choice["current_candidate"] = rebased
        del choice["environment"]
        del choice["prompt"]
        before = fixture.store.validated_snapshot()
        artifacts = {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
        for field, value in (("current_candidate", original), ("current_candidate", "f" * 40)):
            rejected = self.dispatch_native(fixture, choice | {field: value})
            self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected["code"], rejected)
            self.assertEqual(before, fixture.store.validated_snapshot())
            self.assertEqual(
                artifacts,
                {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()},
            )
        correction = self.correction_choice(
            fixture,
            "stale-correction",
            original_context.reference,
            history_id,
            "Reassess the candidate on its new accepted base.",
        )
        rejected = self.dispatch_native(fixture, correction)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", rejected["code"], rejected)
        self.assertEqual(before, fixture.store.validated_snapshot())
        observed_binding = dispatch_operations.observe_correction_candidate(fixture.project)
        assert isinstance(observed_binding, work_brief_models.CorrectionCandidateBinding)
        current_correction = deepcopy(correction)
        current_correction["kind"] = "candidate-correction"
        current_correction["brief_review"] = {
            "schema": "pinboard-current-correction-source-review/v1",
            "boundary": "cross-boundary",
            "source_review": correction["brief_review"],
            "current_candidate": msgspec.to_builtins(observed_binding),
        }
        rejected = fixture.call_advertised_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current_correction},
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", rejected["code"], rejected)
        self.assertEqual(before, fixture.store.validated_snapshot())
        ready = fixture.call_native_tool(
            server.DISPATCH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "dispatch": choice,
            },
        )
        self.assertEqual("replacement-ready", ready["status"], ready)
        self.assertEqual(rebased, ready["candidate_revision"])
        proof_path = fixture.work / str(self.json_object(ready["proof_reference"])["selector"])
        proof = msgspec.json.decode(proof_path.read_bytes(), type=dispatch_operations.ReplacementReadinessProof)
        self.assertEqual(rebased, proof.current_candidate)
        self.assertEqual(
            original_context.reference.content_sha256, proof.source_review.starting_candidate.content_sha256
        )
        lease = self.native_attempt_acquire(fixture, "rebased-worker")
        submitted = self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
            {"candidate": rebased},
        )
        self.assertEqual("committed", submitted["status"], submitted)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        current = fresh.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert current is not None
        snapshot = candidate_snapshots.verify_candidate_snapshot_context(
            current,
            rebased,
            (fixture.work / current.reference.selector).read_bytes(),
        ).snapshot
        self.assertEqual(new_base, snapshot.accepted_base_revision)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        (attempt_root / "result.md").write_bytes(b"Complete rebased result on the new accepted base.\n")
        (attempt_root / "review.md").write_bytes(
            b"New candidate satisfies the replacement; historical finding revalidated.\n"
        )
        job = self.review_result(
            fixture,
            {
                "kind": "correction",
                "candidate_revision": rebased,
                "correction_history_id": history_id,
            },
        )
        self.assertEqual("ready", job["status"], job)
        old_prompt = original_verdict["reviewer_prompt_sha256"]
        assert isinstance(old_prompt, str)
        stale = fixture.call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": self.ready_review_request(fixture, rebased, "new-reviewer", old_prompt),
            },
        )
        self.assertEqual("REVIEWER_PROMPT_NOT_COMMISSIONED", stale["code"], stale)
        prompt = self.json_object(job["prompt_reference"])["sha256"]
        assert isinstance(prompt, str)
        before = fresh.validated_snapshot()
        current_request = self.ready_review_request(fixture, rebased, "new-reviewer", prompt)
        old_bindings = {
            "candidate_revision": original,
            "candidate_snapshot_sha256": original_context.reference.content_sha256,
            "accepted_brief_sha256": self.json_object(original_verdict["accepted_brief"])["content_sha256"],
            "result_sha256": original_verdict["result_sha256"],
            "review_sha256": original_verdict["review_sha256"],
            "reviewer_prompt_sha256": old_prompt,
        }
        for field, value in old_bindings.items():
            with self.subTest(stale_review_binding=field):
                stale = fixture.call_native_tool(
                    server.REVIEW_JOB_TOOL,
                    {
                        "project_root": str(fixture.project),
                        "work_root": str(fixture.work),
                        "review": current_request | {field: value},
                    },
                )
                self.assertEqual("rejected", stale["status"], stale)
                self.assertFalse(stale["state_changed"], stale)
                self.assertEqual(before, fresh.validated_snapshot())
        recorded = fixture.call_native_tool(
            server.REVIEW_JOB_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "review": self.ready_review_request(fixture, rebased, "new-reviewer", prompt),
            },
        )
        self.assertEqual("recorded", recorded["status"], recorded)
        self.assertEqual(original_snapshot, (fixture.work / original_context.reference.selector).read_bytes())
        self.assertEqual(original_review_bytes, original_review_path.read_bytes())
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        self.run_json_cli(*fixture.common, "export")

    def test_replacement_brief_uses_historical_findings_but_requires_a_new_current_return(self) -> None:
        with (
            patch("pinboard.mcp.job_operations.datetime", wraps=datetime) as boundary_clock,
            patch("pinboard.adapters.dispatch_operations.datetime", wraps=datetime) as dispatch_clock,
            patch("tests.checkpoint_support.datetime", wraps=datetime) as fixture_clock,
        ):
            clock_ticks = count()

            def sampled_time(_: tzinfo) -> datetime:
                return SQLITE_NOW + timedelta(seconds=next(clock_ticks))

            boundary_clock.now.side_effect = sampled_time
            dispatch_clock.now.side_effect = sampled_time
            fixture_clock.now.return_value = SQLITE_NOW
            for publish_ready, failure in (
                (False, "infrastructure"),
                (True, "infrastructure"),
                (True, "source"),
                (True, "candidate"),
            ):
                with self.subTest(publish_ready=publish_ready, failure=failure):
                    self.replacement_brief_recovery(publish_ready, failure)

    def replacement_readiness_checks(self, fixture: CheckpointFixture, choice: JsonObject, failure: str) -> None:  # noqa: PLR0915 - one ordered rejection/publication recovery matrix
        mutations: tuple[tuple[tuple[str, ...], JsonValue], ...] = (
            (("checkpoint_id",), "wrong-checkpoint"),
            (("current_candidate",), "f" * 40),
            (("current_candidate",), "working-tree-state-sha256:" + "f" * 64),
            (("correction_history_id",), 999999),
            (("receipt", "subject_revision"), "1"),
            (("brief_review", "contract_review", "reviewer_task_id"), fixture.brief.owner_task_id),
            (("brief_review", "contract_review", "coverage"), []),
            (("brief_review", "contract_review", "accepted_brief_sha256"), "f" * 64),
            (("brief_review", "starting_candidate", "content_sha256"), "f" * 64),
            (("brief_review", "starting_candidate", "size_bytes"), 1),
            (("brief_review", "correction_input", "reason"), "Another return's reason."),
        )
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        files = {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
        for keys, value in mutations:
            with self.subTest(invalid=keys):
                changed = deepcopy(choice)
                target = changed
                for key in keys[:-1]:
                    target = self.json_object(target[key])
                target[keys[-1]] = value
                rejected = self.dispatch_native(fixture, changed)
                self.assertEqual("rejected", rejected["status"], rejected)
                self.assertFalse(rejected["state_changed"], rejected)
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                self.assertEqual(
                    files,
                    {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()},
                )
        for relative in ("new-authority.md", "tests/test_only.py"):
            selected = fixture.project / relative
            original = selected.read_bytes()
            selected.write_bytes(b"changed candidate\n")
            rejected = self.dispatch_native(fixture, choice)
            self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected["code"], rejected)
            self.assertFalse(rejected["state_changed"])
            selected.write_bytes(original)
        publish = dispatch_operations.publish_dispatch_review
        changed_path = fixture.project / ("new-authority.md" if failure == "source" else "tests/test_only.py")
        original = changed_path.read_bytes()

        def second_publication_fails(
            store: WorkStore,
            artifacts: DispatchArtifactPort,
            attempt_id: AttemptId,
            key: str,
            content: bytes,
            review_id: ReviewId,
            accepted_at: datetime,
        ) -> (
            application_dispatch.DispatchResult[application_dispatch.AcceptedDispatchReview]
            | ArtifactAcceptanceFailure
            | ArtifactWriteFailure
        ):
            if not key.startswith("replacement-readiness-") and failure == "infrastructure":
                raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "plain readiness publication failed")
            result = publish(store, artifacts, attempt_id, key, content, review_id, accepted_at)
            if key.startswith("replacement-readiness-") and failure in ("source", "candidate"):
                changed_path.write_bytes(b"Controlled change after proof publication.\n")
            return result

        with patch(
            "pinboard.adapters.dispatch_operations.publish_dispatch_review", side_effect=second_publication_fails
        ):
            failed = self.dispatch_native(fixture, choice)
        self.assertEqual("failed-after-publication", failed["status"], failed)
        self.assertEqual("do-not-retry", failed["retry"])
        self.assertEqual(["immutable-artifact", "accepted-artifact-reference", "ledger"], failed["changed_surfaces"])
        if failure in ("source", "candidate"):
            self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", failed["code"], failed)
            changed_path.write_bytes(original)
        fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
        self.assertIsNone(
            fresh.read_artifact_reference(
                work_models.ArtifactKind.EVIDENCE,
                f"work-a-1-brief-review-{work_briefs.ready_review_key_sha256(fixture.brief)}",
                1,
            )
        )

        def pause_after_plain_publication(
            store: WorkStore,
            artifacts: DispatchArtifactPort,
            attempt_id: AttemptId,
            key: str,
            content: bytes,
            review_id: ReviewId,
            accepted_at: datetime,
        ) -> (
            application_dispatch.DispatchResult[application_dispatch.AcceptedDispatchReview]
            | ArtifactAcceptanceFailure
            | ArtifactWriteFailure
        ):
            result = publish(store, artifacts, attempt_id, key, content, review_id, accepted_at)
            if not key.startswith("replacement-readiness-"):
                paused = self.transition_result(
                    fixture, self.project_action(fixture, "pause:work-a-1"), {"reason": "Controlled concurrent action."}
                )
                self.assertEqual("committed", paused["status"], paused)
            return result

        with patch(
            "pinboard.adapters.dispatch_operations.publish_dispatch_review", side_effect=pause_after_plain_publication
        ):
            failed = self.dispatch_native(fixture, choice)
        self.assertEqual("failed-after-publication", failed["status"], failed)
        self.assertEqual("do-not-retry", failed["retry"])
        self.assertEqual(["immutable-artifact", "accepted-artifact-reference", "ledger"], failed["changed_surfaces"])
        self.pause_and_resume(fixture, 0)
        self.assertEqual(
            "committed", self.transition_result(fixture, self.project_action(fixture, "resume:work-a"), {})["status"]
        )
        action = self.project_action(fixture, "dispatch:work-a-1")
        choice["receipt"] = {"action_id": action["action_id"], "subject_revision": action["subject_revision"]}

    def replacement_brief_recovery(self, publish_ready: bool, failure: str) -> None:  # noqa: PLR0915 - one complete causal recovery journey
        fixture = self.correction_fixture()
        old_history, old_choice = self.submit_and_return(fixture, "returned-test", committed=True)
        # Candidate sources moved, removed and added after base; its accepted snapshot owns those bytes.
        (fixture.project / "architecture.md").rename(fixture.project / "replacement.md")
        (fixture.project / "new-authority.md").write_text("New candidate authority.\n", encoding="utf-8")
        candidate = self.commit_all(fixture.project, "moved and newly added candidate authorities")
        lease = self.native_attempt_acquire(fixture, "structural-return")
        self.assertEqual(
            "committed",
            self.transition_result(
                fixture,
                self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
                {"candidate": candidate},
            )["status"],
        )
        returned = self.transition_result(
            fixture,
            self.project_action(fixture, "return-for-correction:work-a-1"),
            {"reason": "Repair the test-only candidate."},
        )
        old_history = returned["history_id"]
        assert isinstance(old_history, int)
        old_choice["correction_history_id"] = old_history
        checkpoint = fixture.brief.checkpoint
        assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
        moved = msgspec.structs.replace(checkpoint.reviewed_authorities[0], selector="replacement.md#Contract")
        added = msgspec.structs.replace(
            moved,
            authority_id="new-authority",
            selector="new-authority.md",
            reviewed_sha256=hashlib.sha256((fixture.project / "new-authority.md").read_bytes()).hexdigest(),
        )
        replacement = msgspec.structs.replace(
            fixture.brief,
            artifact_revision=fixture.brief.artifact_revision + 1,
            checkpoint=msgspec.structs.replace(
                checkpoint,
                title="Review a genuinely distinct replacement checkpoint",
                reviewed_authorities=(moved, added),
                coverage=(
                    *checkpoint.coverage,
                    msgspec.structs.replace(checkpoint.coverage[0], authority_id="new-authority"),
                ),
            ),
            bootstrap=(
                *fixture.brief.bootstrap,
                "Reassess the protected candidate against the complete replacement brief.",
            ),
        )
        published = fixture.call_native_tool(
            server.BRIEF_PUBLISH_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "brief": msgspec.to_builtins(replacement),
            },
        )
        self.assertEqual("committed", published["status"], published)
        brief_reference = self.json_object(published["reference"])
        rebound = self.transition_result(
            fixture,
            self.project_action(fixture, "rebind-attempt:work-a-1"),
            {
                "branch": replacement.branch,
                "base_revision": replacement.base_revision,
                "brief_artifact_ref_id": brief_reference["artifact_ref_id"],
            },
        )
        self.assertEqual("committed", rebound["status"], rebound)
        fixture = dataclasses.replace(fixture, brief=replacement)
        ready_key = f"work-a-1-brief-review-{work_briefs.ready_review_key_sha256(replacement)}"
        self.assertIsNone(fixture.store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, ready_key, 1))
        action = self.project_action(fixture, "dispatch:work-a-1")
        old_choice["receipt"] = {
            "action_id": {"kind": "dispatch", "subject": "work-a-1"},
            "subject_revision": action["subject_revision"],
        }
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        stale = fixture.call_native_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": old_choice},
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", stale["code"], stale)
        self.assertFalse(stale["state_changed"])
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        replacement_choice: JsonObject | None = None
        if publish_ready:
            ordinary = deepcopy(old_choice)
            ordinary["kind"] = "reviewed"
            ordinary["brief_review"] = msgspec.json.decode(ready_review(replacement))
            del ordinary["correction_history_id"]
            rejected = self.dispatch_native(fixture, ordinary)
            self.assertEqual("DISPATCH_AUTHORITY_UNREADABLE", rejected["code"], rejected)
            self.assertFalse(rejected["state_changed"])
            returned_context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_review_job_context(
                AttemptId("work-a-1"), None, HistoryId(old_history), None, None
            )
            assert returned_context is not None and returned_context.returned_candidate_snapshot is not None
            replacement_choice = self.correction_choice(
                fixture,
                "replacement-readiness",
                returned_context.returned_candidate_snapshot.reference,
                old_history,
                "Repair the test-only candidate.",
            )
            replacement_choice["kind"] = "replacement-readiness"
            replacement_choice["current_candidate"] = candidate
            replacement_review = self.json_object(replacement_choice["brief_review"])
            self.json_object(replacement_review["contract_review"])["reviewer_task_id"] = "replacement-reviewer"
            replacement_review["assessment"] = (
                "The unchanged returned structural candidate satisfies the complete replacement contracts."
            )
            del replacement_choice["environment"]
            del replacement_choice["prompt"]
            self.replacement_readiness_checks(fixture, replacement_choice, failure)
            published_ready = fixture.call_native_tool(
                server.DISPATCH_TOOL,
                {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "dispatch": replacement_choice,
                },
            )
            self.assertEqual("replacement-ready", published_ready["status"], published_ready)
            self.assertNotIn("native_launch", published_ready)
            self.assertNotIn("prompt_reference", published_ready)
            proof = self.json_object(published_ready["proof_reference"])
            proof_key, proof_revision = proof["key"], proof["revision"]
            assert isinstance(proof_key, str) and isinstance(proof_revision, int)
            accepted = SQLiteWorkStore(fixture.work / "state.sqlite3").read_artifact_reference(
                work_models.ArtifactKind.EVIDENCE, proof_key, proof_revision
            )
            assert accepted is not None
            self.assertEqual(proof["sha256"], accepted.content_sha256)
            self.assertEqual(
                msgspec.json.decode(msgspec.json.encode(replacement_choice["brief_review"])),
                self.json_object(json.loads((fixture.work / accepted.selector).read_bytes()))["source_review"],
            )
            reused = self.dispatch_native(fixture, replacement_choice)
            self.assertEqual("replacement-ready", reused["status"], reused)
            self.assertFalse(reused["state_changed"])
            collision = deepcopy(replacement_choice)
            collision["review_id"] = "different-replacement-assessment"
            self.json_object(collision["brief_review"])["assessment"] = "A differing independent assessment."
            rejected = self.dispatch_native(fixture, collision)
            self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", rejected["code"], rejected)
            self.assertEqual("do-not-retry", rejected["retry"])
            self.assertTrue(rejected["state_changed"])
        ready_reference = SQLiteWorkStore(fixture.work / "state.sqlite3").read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE, ready_key, 1
        )
        self.assertEqual(publish_ready, ready_reference is not None)

        lease = self.native_attempt_acquire(fixture, "replacement-owner")
        submitted = self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease),
            {"candidate": candidate},
        )
        self.assertEqual("committed", submitted["status"], submitted)
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        historical_review = self.review_result(
            fixture,
            {"kind": "correction", "candidate_revision": candidate, "correction_history_id": old_history},
        )
        self.assertEqual("ready", historical_review["status"], historical_review)
        self.assertEqual(candidate, historical_review["candidate_revision"])
        self.assertEqual(brief_reference["sha256"], historical_review["brief_sha256"])
        self.assertEqual(old_history, self.json_object(historical_review["review_round"])["history_id"])
        denied_dispatch = self.actions_result(
            fixture, {"role": "project", "action_id": {"kind": "dispatch", "subject": "work-a-1"}}
        )
        self.assertFalse(denied_dispatch.get("actions"), denied_dispatch)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3")
        context = reloaded.read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        protected = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / context.reference.selector).read_bytes()
        )
        self.assertEqual(candidate, protected.candidate)
        current = reloaded.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(current, query_models.NonterminalAttemptContextFacts)
        self.assertEqual(brief_reference["artifact_ref_id"], int(current.brief_artifact_ref_id))

        # A deterministic reviewer verdict is evidence input, not a prose-semantic assertion.
        fresh_verdict = b"Complete replacement-brief review: prior finding revalidated; correction remains required.\n"
        (fixture.work / "attempts" / "work-a-1" / "review.md").write_bytes(fresh_verdict)
        reason = "Repair the test-only candidate."
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": reason}
        )
        self.assertEqual("committed", returned["status"], returned)
        new_history = returned["history_id"]
        assert isinstance(new_history, int)
        self.assertNotEqual(old_history, new_history)
        if publish_ready:
            assert replacement_choice is not None
            current_action = self.project_action(fixture, "dispatch:work-a-1")
            replacement_choice["receipt"] = {
                "action_id": current_action["action_id"],
                "subject_revision": current_action["subject_revision"],
            }
            rejected = self.dispatch_native(fixture, replacement_choice)
            self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", rejected["code"], rejected)
            self.assertFalse(rejected["state_changed"])
        current_choice = self.correction_choice(
            fixture, "replacement-correction", context.reference, new_history, reason
        )
        dispatched = fixture.call_native_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current_choice},
        )
        self.assertEqual("ready", dispatched["status"], dispatched)
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3")
        fresh_context = reloaded.read_review_job_context(
            AttemptId("work-a-1"), None, HistoryId(new_history), None, None
        )
        assert fresh_context is not None and fresh_context.correction_receipt is not None
        self.assertEqual(new_history, int(fresh_context.correction_receipt.history_id))
        self.assertEqual(fresh_verdict, (fixture.work / "attempts" / "work-a-1" / "review.md").read_bytes())
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

        self.assertEqual(
            ready_reference,
            reloaded.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, ready_key, 1),
        )
        (fixture.project / "tests" / "test_only.py").write_text("assert 'corrected'\n", encoding="utf-8")
        corrected = self.commit_all(fixture.project, "corrected replacement candidate")
        worker_lease = self.native_attempt_acquire(fixture, "replacement-correction-worker")
        submitted = self.transition_result(
            fixture,
            self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=worker_lease),
            {"candidate": corrected},
        )
        self.assertEqual("committed", submitted["status"], submitted)
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])
        corrected_review = self.review_result(
            fixture, {"kind": "correction", "candidate_revision": corrected, "correction_history_id": new_history}
        )
        self.assertEqual("ready", corrected_review["status"], corrected_review)
        attempt_root = fixture.work / "attempts" / "work-a-1"
        result_bytes = b"Complete corrected frozen result.\n"
        review_bytes = b"Complete favorable current candidate verdict.\n"
        (attempt_root / "result.md").write_bytes(result_bytes)
        (attempt_root / "review.md").write_bytes(review_bytes)
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        files = {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
        outcome = self.transition_result(
            fixture,
            self.project_action(fixture, "accept-checkpoint:work-a-1"),
            {
                "checkpoint": replacement.checkpoint.checkpoint_id,
                "candidate": corrected,
                "evidence": "Exact favorable corrected review.",
            },
        )
        fresh_store = SQLiteWorkStore(fixture.work / "state.sqlite3")
        if not publish_ready:
            self.assertEqual(
                ("rejected", "TRANSITION_INPUT_INVALID", False, "unchanged", []),
                (
                    outcome["status"],
                    outcome["code"],
                    outcome["state_changed"],
                    outcome["effect"],
                    outcome["changed_surfaces"],
                ),
            )
            message = outcome["message"]
            assert isinstance(message, str)
            self.assertIn("exact ready brief review", message)
            self.assertEqual(before, fresh_store.validated_snapshot())
            self.assertEqual(
                files, {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
            )
            return
        self.assertEqual("committed", outcome["status"], outcome)
        current = fresh_store.read_attempt_context(AttemptId("work-a-1"))
        assert isinstance(current, query_models.NonterminalAttemptContextFacts)
        self.assertEqual(work_models.AttemptState.PAUSED, current.state)
        self.assertIsNone(current.candidate_revision)
        package_reference = fresh_store.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            artifact_identity.artifact_key(
                artifact_identity.CheckpointAddress(
                    AttemptId("work-a-1"),
                    CheckpointId(replacement.checkpoint.checkpoint_id),
                    artifact_identity.CheckpointRole.PACKAGE,
                    1,
                )
            ),
            1,
        )
        assert package_reference is not None and ready_reference is not None
        package = work_briefs.decode_canonical_checkpoint_review_package(
            (fixture.work / package_reference.selector).read_bytes()
        )
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        assert isinstance(package.review_basis, work_brief_models.CrossBoundaryReviewBasis)
        self.assertEqual(corrected, package.candidate)
        self.assertEqual(ready_reference.selector, package.review_basis.brief_review.selector)
        self.assertEqual(result_bytes, (fixture.work / package.result.selector).read_bytes())
        self.assertEqual(review_bytes, (fixture.work / package.implementation_review.selector).read_bytes())
        self.assertTrue(self.run_json_cli(*fixture.common, "validate")["valid"])

    def test_old_same_patch_snapshot_cannot_authorize_a_newer_returned_start(self) -> None:
        fixture = self.correction_fixture()
        first_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        _, first = self.submit_and_return(fixture, "same-test", committed=False)
        first_candidate = root.read_working_tree_candidate(fixture.project)
        test_file = fixture.project / "tests" / "test_only.py"
        test_file.write_text("assert True\n", encoding="utf-8")
        (fixture.project / "tracked.txt").write_text("another committed state\n", encoding="utf-8")
        second_head = self.commit_all(fixture.project, "different starting code")
        _, second = self.submit_and_return(fixture, "same-test", committed=False)
        second_candidate = root.read_working_tree_candidate(fixture.project)
        self.assertNotEqual(first_head, second_head)
        self.assertEqual(first_candidate.diff, second_candidate.diff)
        old_snapshot = deepcopy(second)
        self.json_object(old_snapshot["brief_review"])["starting_candidate"] = self.json_object(first["brief_review"])[
            "starting_candidate"
        ]
        test_file.write_text("assert True\n", encoding="utf-8")
        subprocess.run(["git", "switch", "--detach", first_head], cwd=fixture.project, check=True, capture_output=True)
        subprocess.run(
            ["git", "switch", "-C", fixture.brief.branch], cwd=fixture.project, check=True, capture_output=True
        )
        test_file.write_text("assert 'same-test'\n", encoding="utf-8")
        before = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        artifacts = {
            path.relative_to(fixture.work): path.read_bytes()
            for path in (fixture.work / "artifacts").rglob("*")
            if path.is_file()
        }
        outcome = self.dispatch_native(fixture, old_snapshot)
        self.assertNotEqual("ready", outcome["status"], outcome)
        self.assertFalse(outcome["state_changed"])
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        self.assertEqual(
            artifacts,
            {
                path.relative_to(fixture.work): path.read_bytes()
                for path in (fixture.work / "artifacts").rglob("*")
                if path.is_file()
            },
        )

    def test_real_native_two_test_only_candidates_reload_distinct_subjects_and_initial_proof(self) -> None:
        fixture = self.correction_fixture()
        initial_key = f"work-a-1-brief-review-{work_briefs.ready_review_key_sha256(fixture.brief)}"
        initial_reference = fixture.store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, initial_key, 1)
        assert initial_reference is not None
        initial_bytes = (fixture.work / initial_reference.selector).read_bytes()
        first_history, first = self.submit_and_return(fixture, "first-test", committed=False)
        first_dispatch = self.dispatch_native(fixture, first)
        self.assertEqual("ready", first_dispatch["status"])
        first_reference = self.json_object(first_dispatch["prompt_reference"])
        first_prompt = (fixture.work / str(first_reference["selector"])).read_text()
        self.assertIn(f"Selected return history ID: {first_history}", first_prompt)
        self.assertIn('Canonical return reason (JSON string): "Repair the test-only candidate."', first_prompt)
        self.assertIn(
            f"Read current review evidence before editing: {fixture.work / 'attempts' / 'work-a-1' / 'review.md'}",
            first_prompt,
        )
        first_state = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        first_reviews = {value.key for value in first_state.artifact_references if "-brief-review-" in value.key}
        replay = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), first, mcp_execution.CancellationToken()
        )
        self.assertEqual("unchanged", replay.content["effect"], replay.content)
        second_history, second = self.submit_and_return(fixture, "second-test", committed=False)
        self.assertEqual(
            self.json_object(first["brief_review"])["contract_review"],
            self.json_object(second["brief_review"])["contract_review"],
        )
        ready = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), second, mcp_execution.CancellationToken()
        )
        self.assertEqual("ready", ready.content["status"], ready.content)
        second_reference = self.json_object(ready.content["prompt_reference"])
        second_prompt = (fixture.work / str(second_reference["selector"])).read_text()
        self.assertIn(f"Selected return history ID: {second_history}", second_prompt)
        self.assertNotIn(f"Selected return history ID: {first_history}", second_prompt)
        second_state = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        second_reviews = {value.key for value in second_state.artifact_references if "-brief-review-" in value.key}
        self.assertEqual(1, len(second_reviews - first_reviews))
        self.assertEqual(initial_bytes, (fixture.work / initial_reference.selector).read_bytes())
        collision = deepcopy(second)
        self.json_object(collision["brief_review"])["assessment"] = (
            "Different evidence about the actually identical complete subject."
        )
        collision["review_id"] = "different-proof"
        outcome = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), collision, mcp_execution.CancellationToken()
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", outcome.content["code"])
        self.assertEqual("failed-after-publication", outcome.content["status"])
        self.assertEqual("do-not-retry", outcome.content["retry"])
        replay_collision = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), collision, mcp_execution.CancellationToken()
        )
        self.assertFalse(replay_collision.content["state_changed"])

    def test_wrong_snapshot_reason_candidate_preimage_and_stale_return_reject_before_publication(self) -> None:
        fixture = self.correction_fixture()
        first_history, first = self.submit_and_return(fixture, "first-test", committed=False)
        _, choice = self.submit_and_return(fixture, "second-test", committed=False)
        fixture = self.pause_and_resume(fixture, 1)
        action = self.project_action(fixture, "dispatch:work-a-1")
        choice["receipt"] = {"action_id": action["action_id"], "subject_revision": action["subject_revision"]}
        changed_reason = deepcopy(choice)
        self.json_object(self.json_object(changed_reason["brief_review"])["correction_input"])["reason"] = "Wrong fix."
        changed_identity = deepcopy(choice)
        self.json_object(self.json_object(changed_identity["brief_review"])["starting_candidate"])["content_sha256"] = (
            "a" * 64
        )
        wrong_candidate = deepcopy(choice)
        self.json_object(wrong_candidate["brief_review"])["starting_candidate"] = self.json_object(
            first["brief_review"]
        )["starting_candidate"]
        stale_history = choice | {"correction_history_id": first_history}
        for invalid in (changed_reason, changed_identity, wrong_candidate, stale_history):
            with self.subTest(invalid=invalid):
                before = fixture.store.validated_snapshot()
                outcome = self.dispatch_native(fixture, invalid)
                self.assertNotEqual("ready", outcome["status"], outcome)
                self.assertFalse(outcome["state_changed"])
                self.assertEqual(before, fixture.store.validated_snapshot())
        self.commit_all(fixture.project, "different preimage")
        before = fixture.store.validated_snapshot()
        rejected = mcp_jobs._dispatch_job(
            str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected.content["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_native_correction_context_binds_fresh_review_before_dispatch(self) -> None:
        fixture = self.correction_fixture()
        (fixture.project / "architecture.md").write_text(
            "# Architecture\n\n## Contract\n\nTyped JSON stays canonical after correction.\n", encoding="utf-8"
        )
        history_id, choice = self.submit_and_return(fixture, "changed-source", committed=True)
        before = fixture.store.validated_snapshot()
        artifact_files = {
            path.relative_to(fixture.work): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (fixture.work / "artifacts").rglob("*")
            if path.is_file()
        }
        context = fixture.call_native_tool(
            server.CORRECTION_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "correction_history_id": history_id,
            },
        )
        self.assertEqual("ready", context["status"], context)
        identity = self.json_object(context["starting_candidate"])
        snapshot_bytes = (fixture.work / str(identity["selector"])).read_bytes()
        self.assertEqual(identity["content_sha256"], hashlib.sha256(snapshot_bytes).hexdigest())
        accepted_snapshot = candidate_snapshots.decode_candidate_snapshot(snapshot_bytes)
        snapshot = self.json_object(context["starting_snapshot"])
        self.assertEqual(accepted_snapshot.candidate, snapshot["candidate"])
        self.assertEqual(accepted_snapshot.diff, base64.b64decode(str(snapshot["diff_base64"]), validate=True))
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual(
            artifact_files,
            {
                path.relative_to(fixture.work): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (fixture.work / "artifacts").rglob("*")
                if path.is_file()
            },
        )
        effective = msgspec.convert(context["effective_brief"], type=work_brief_models.WorkBrief, strict=True)
        self.assertEqual(
            context["effective_brief_sha256"],
            hashlib.sha256(work_briefs.canonical_work_brief_bytes(effective)).hexdigest(),
        )
        self.assertNotEqual(
            context["effective_brief_sha256"],
            hashlib.sha256(work_briefs.canonical_work_brief_bytes(fixture.brief)).hexdigest(),
        )
        review = self.json_object(choice["brief_review"])
        review["contract_review"] = msgspec.json.decode(ready_review(effective))
        review["starting_candidate"] = context["starting_candidate"]
        review["correction_input"] = {"reason": context["correction_reason"]}
        stale = deepcopy(choice)
        self.json_object(stale["brief_review"])["contract_review"] = msgspec.json.decode(ready_review(fixture.brief))
        rejected = self.dispatch_native(fixture, stale)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        wrong_snapshot = deepcopy(choice)
        self.json_object(self.json_object(wrong_snapshot["brief_review"])["starting_candidate"])["content_sha256"] = (
            "f" * 64
        )
        rejected = self.dispatch_native(fixture, wrong_snapshot)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        self.assertEqual("ready", self.dispatch_native(fixture, choice)["status"])

    def test_dirty_commit_and_late_checkout_change_preserve_truthful_effects(self) -> None:
        fixture = self.correction_fixture()
        _, choice = self.submit_and_return(fixture, "committed-test", committed=True)
        test_file = fixture.project / "tests" / "test_only.py"
        exact = test_file.read_bytes()
        test_file.write_bytes(b"assert 'dirty'\n")
        before = fixture.store.validated_snapshot()
        rejected = self.dispatch_native(fixture, choice)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())
        test_file.write_bytes(exact)
        observe = dispatch_operations._read_correction_start
        calls = 0

        def change_before_recheck(
            store: WorkStore,
            artifacts: DispatchArtifactPort,
            source_checkout_root: Path,
            brief: work_brief_models.WorkBrief,
            dispatch: dispatch_operations.CorrectionDispatch,
        ) -> dispatch_operations.DispatchResult[candidate_snapshots.CandidateSnapshot]:
            nonlocal calls
            calls += 1
            if calls == 2:
                test_file.write_bytes(b"assert 'changed during publication'\n")
            return observe(store, artifacts, source_checkout_root, brief, dispatch)

        with patch.object(dispatch_operations, "_read_correction_start", side_effect=change_before_recheck):
            outcome = mcp_jobs._dispatch_job(
                str(fixture.project), str(fixture.work), choice, mcp_execution.CancellationToken()
            )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", outcome.content["code"])
        self.assertEqual("failed-after-publication", outcome.content["status"])
        self.assertEqual("do-not-retry", outcome.content["retry"])
        after = fixture.store.validated_snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertGreater(len(after.artifact_references), len(before.artifact_references))

    def test_correction_requires_complete_strict_record_and_initial_review_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            initial = ready_review(work_a_brief(Path(directory)))
            self.assertIsInstance(work_briefs.decode_work_brief_review(initial), work_brief_models.WorkBriefReview)

        fixture = self.correction_fixture()
        _history_id, choice = self.submit_and_return(fixture, "strict-record", committed=False)
        changes: tuple[dict[str, work_brief_models.WorkBriefJsonValue], ...] = (
            {"unknown": True},
            {"starting_candidate": {}},
            {"correction_input": {"reason": ""}},
        )
        for change in changes:
            with self.subTest(change=change):
                invalid = deepcopy(choice)
                self.json_object(invalid["brief_review"]).update(change)
                before = fixture.store.validated_snapshot()
                rejected = self.dispatch_native(fixture, invalid)
                self.assertEqual("DISPATCH_INVALID", rejected["code"])
                self.assertFalse(rejected["state_changed"])
                self.assertEqual(before, fixture.store.validated_snapshot())

    def current_candidate_choice(self, fixture: CheckpointFixture, history_id: int, choice: JsonObject) -> JsonObject:
        context = fixture.call_advertised_tool(
            server.RETURN_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "selection": {"kind": "current-candidate", "return_history_id": history_id},
            },
        )
        self.assertEqual("ready", context["status"], context)
        selected = self.json_object(context["context"])
        correction = self.json_object(selected["review_context"])
        result = deepcopy(choice)
        source = self.json_object(result["brief_review"])
        effective = msgspec.convert(correction["effective_brief"], type=work_brief_models.WorkBrief, strict=True)
        source["contract_review"] = msgspec.json.decode(ready_review(effective))
        source["starting_candidate"] = correction["starting_candidate"]
        source["correction_input"] = {"reason": correction["correction_reason"]}
        result["kind"] = "candidate-correction"
        result["brief_review"] = {
            "schema": "pinboard-current-correction-source-review/v1",
            "boundary": "cross-boundary",
            "source_review": source,
            "current_candidate": selected["current_candidate"],
        }
        return result

    def test_advertised_candidate_correction_accepts_tracked_and_untracked_drift(self) -> None:
        for tracked_drift in (False, True):
            with self.subTest(tracked_drift=tracked_drift):
                fixture = self.correction_fixture()
                history_id, choice = self.submit_and_return(fixture, "returned", committed=True)
                scratch = fixture.project / "scratch.txt"
                scratch.write_text("untracked content is not candidate identity\n")
                if tracked_drift:
                    (fixture.project / "tests" / "test_only.py").write_text("assert 'reviewed drift'\n")
                before = fixture.store.validated_snapshot()
                ordinary = fixture.call_advertised_tool(
                    server.DISPATCH_TOOL,
                    {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": choice},
                )
                self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", ordinary["code"], ordinary)
                self.assertEqual(before, fixture.store.validated_snapshot())
                current = self.current_candidate_choice(fixture, history_id, choice)
                binding = self.json_object(self.json_object(current["brief_review"])["current_candidate"])
                self.assertEqual(["scratch.txt"], binding["omitted_untracked_paths"])
                scratch.write_text("different omitted bytes\n")
                ready = fixture.call_advertised_tool(
                    server.DISPATCH_TOOL,
                    {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current},
                )
                self.assertEqual("ready", ready["status"], ready)
                prompt_reference = self.json_object(ready["prompt_reference"])
                prompt = (fixture.work / str(prompt_reference["selector"])).read_bytes()
                self.assertEqual(prompt_reference["sha256"], hashlib.sha256(prompt).hexdigest())
                self.assertIn(msgspec.json.encode(binding, order="sorted"), prompt)
                fresh = SQLiteWorkStore(fixture.work / "state.sqlite3")
                accepted = [
                    ref
                    for ref in fresh.validated_snapshot().artifact_references
                    if b"pinboard-current-correction-source-review/v1" in (fixture.work / ref.selector).read_bytes()
                ]
                self.assertEqual(1, len(accepted))
                evidence = msgspec.json.decode(
                    (fixture.work / accepted[0].selector).read_bytes(), type=work_brief_models.CurrentCorrectionReview
                )
                self.assertEqual(binding, msgspec.json.decode(msgspec.json.encode(evidence.current_candidate)))

    def test_current_candidate_binding_and_independent_source_gates_reject_before_effects(self) -> None:
        fixture = self.correction_fixture()
        history_id, exact = self.submit_and_return(fixture, "returned", committed=False)
        tracked = fixture.project / "tests" / "test_only.py"
        tracked.write_text("assert 'reviewed drift'\n")
        current = self.current_candidate_choice(fixture, history_id, exact)
        binding = self.json_object(self.json_object(current["brief_review"])["current_candidate"])
        wrong = deepcopy(current)
        self.json_object(self.json_object(wrong["brief_review"])["current_candidate"])["candidate"] = (
            "working-tree-state-sha256:" + "f" * 64
        )
        wrong_reason = deepcopy(current)
        self.json_object(
            self.json_object(self.json_object(wrong_reason["brief_review"])["source_review"])["correction_input"]
        )["reason"] = "wrong reason"
        wrong_identity = deepcopy(current)
        self.json_object(
            self.json_object(self.json_object(wrong_identity["brief_review"])["source_review"])["starting_candidate"]
        )["content_sha256"] = "f" * 64
        for invalid in (wrong, wrong_reason, wrong_identity):
            before = fixture.store.validated_snapshot()
            result = self.dispatch_native(fixture, invalid)
            self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", result["code"], result)
            self.assertFalse(result["state_changed"])
            self.assertEqual(before, fixture.store.validated_snapshot())
        scratch = fixture.project / "scratch.txt"
        scratch.write_text("new omitted name\n")
        before = fixture.store.validated_snapshot()
        result = self.dispatch_native(fixture, current)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", result["code"], result)
        self.assertEqual(before, fixture.store.validated_snapshot())
        scratch.unlink()
        source = fixture.project / "architecture.md"
        previous = source.read_bytes()
        source.write_bytes(previous + b"Changed reviewed source.\n")
        result = self.dispatch_native(fixture, current)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", result["code"], result)
        source.write_bytes(previous)
        subprocess.run(["git", "switch", "-c", "wrong-branch"], cwd=fixture.project, check=True, capture_output=True)
        result = self.dispatch_native(fixture, current)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", result["code"], result)
        subprocess.run(["git", "switch", fixture.brief.branch], cwd=fixture.project, check=True, capture_output=True)
        self.commit_all(fixture.project, "changed returned HEAD")
        self.json_object(self.json_object(current["brief_review"])["current_candidate"])["candidate"] = (
            root.read_working_tree_candidate(fixture.project).identity
        )
        result = self.dispatch_native(fixture, current)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", result["code"], result)
        self.assertEqual([], binding["omitted_untracked_paths"])

    def test_current_candidate_publication_rechecks_paths_and_reports_committed_effects(self) -> None:
        for publication_stage in (2, 3):
            with self.subTest(publication_stage=publication_stage):
                self.check_publication_recheck(publication_stage)

    def check_publication_recheck(self, publication_stage: int) -> None:
        fixture = self.correction_fixture()
        history_id, exact = self.submit_and_return(fixture, "returned", committed=True)
        current = self.current_candidate_choice(fixture, history_id, exact)
        observed = dispatch_operations._read_correction_start
        calls = 0

        def change_after_review(
            store: WorkStore,
            artifacts: DispatchArtifactPort,
            source_checkout_root: Path,
            brief: work_brief_models.WorkBrief,
            choice: dispatch_operations.CorrectionPreparation,
        ) -> dispatch_operations.DispatchResult[candidate_snapshots.CandidateSnapshot]:
            nonlocal calls
            calls += 1
            if calls == publication_stage:
                (fixture.project / "late-scratch.txt").write_text("late omitted name\n")
            return observed(store, artifacts, source_checkout_root, brief, choice)

        before = fixture.store.validated_snapshot()
        with patch.object(dispatch_operations, "_read_correction_start", side_effect=change_after_review):
            result = self.dispatch_native(fixture, current)
        self.assertEqual("failed-after-publication", result["status"], result)
        self.assertEqual("do-not-retry", result["retry"])
        after = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertGreater(len(after.artifact_references), len(before.artifact_references))

    def test_historical_return_read_rejects_wrong_absent_and_damaged_selected_evidence(self) -> None:
        fixture = self.correction_fixture()
        history_id, choice = self.submit_and_return(fixture, "returned", committed=True)
        arguments: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "attempt_id": "work-a-1",
            "selection": {"kind": "historical", "return_history_id": history_id},
        }
        returned = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
        selected = self.json_object(returned["context"])
        before = fixture.store.validated_snapshot()
        absent_selection: JsonObject = {"kind": "historical", "return_history_id": 999999}
        invalid_requests: tuple[JsonObject, ...] = (
            arguments | {"attempt_id": "work-b-1"},
            arguments | {"selection": absent_selection},
        )
        for invalid in invalid_requests:
            result = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, invalid)
            self.assertEqual("rejected", result["status"], result)
            self.assertEqual(before, fixture.store.validated_snapshot())
        reference = self.json_object(selected["starting_candidate"])
        path = fixture.work / str(reference["selector"])
        original = path.read_bytes()
        path.write_bytes(b"damaged selected snapshot\n")
        result = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
        self.assertEqual("DISPATCH_BRIEF_REVIEW_INVALID", result["code"], result)
        self.assertFalse(result["state_changed"])
        path.write_bytes(original)
        self.assertEqual(before, fixture.store.validated_snapshot())

        for change in ({"status": "drifted"}, {"schema": "pinboard-return-context/v99"}, {"unknown": True}):
            with self.assertRaises(msgspec.ValidationError):
                contract_schemas.validate_result(server.RETURN_CONTEXT_TOOL, returned | change)
        invalid = deepcopy(choice)
        invalid["kind"] = "candidate-correction"
        invalid["brief_review"] = {
            "schema": "pinboard-current-correction-source-review/v1",
            "boundary": "unknown",
            "source_review": choice["brief_review"],
            "current_candidate": {"candidate": "f" * 64, "omitted_untracked_paths": ["scratch.txt"]},
        }
        self.assertEqual("DISPATCH_INVALID", self.dispatch_native(fixture, invalid)["code"])

    def test_historical_read_preserves_selected_evidence_with_unrelated_damaged_receipts_before_and_after_return(
        self,
    ) -> None:
        fixture = self.correction_fixture()
        (fixture.project / "tests" / "test_only.py").write_text("assert 'returned'\n", encoding="utf-8")
        candidate = self.commit_all(fixture.project, "returned")
        lease = self.native_attempt_acquire(fixture, "returned")
        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
        payload = fixture.work / "submit.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        submission = self.transition_json(fixture, action, payload)
        submission_history_id = submission["history_id"]
        assert isinstance(submission_history_id, int)
        database = fixture.work / "state.sqlite3"

        def add_unrelated_damaged_receipts(template_history_id: int) -> None:
            connection = sqlite3.connect(database)
            try:
                last = connection.execute(
                    "SELECT MAX(history_id), MAX(project_revision) FROM transition_history"
                ).fetchone()
                assert last is not None
                for index in range(1, 201):
                    connection.execute(
                        """INSERT INTO transition_history (
                            history_id, project_revision, action_id, action_kind, subject_id,
                            artifact_ref_id, artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                            input_schema, input_json, outcome_schema, outcome_json, committed_at
                        ) SELECT ?, ?, action_id, action_kind, 'unrelated-attempt', artifact_ref_id, artifact_kind,
                                 authorization_kind, actor_task_id, actor_host_id, input_schema, '{}', outcome_schema,
                                 '{}', committed_at FROM transition_history WHERE history_id = ?""",
                        (last[0] + index, last[1] + index, template_history_id),
                    )
                connection.execute("UPDATE project_meta SET revision = ? WHERE singleton = 1", (last[1] + 200,))
                connection.commit()
            finally:
                connection.close()

        add_unrelated_damaged_receipts(submission_history_id)
        payload.write_text(json.dumps({"reason": "Repair the test-only candidate."}), encoding="utf-8")
        returned = self.transition_json(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload
        )
        history_id = returned["history_id"]
        assert isinstance(history_id, int)
        arguments: JsonObject = {
            "project_root": str(fixture.project),
            "work_root": str(fixture.work),
            "attempt_id": "work-a-1",
            "selection": {"kind": "historical", "return_history_id": history_id},
        }
        baseline = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
        self.assertEqual("ready", baseline["status"], baseline)
        selected = self.json_object(baseline["context"])
        receipt = self.json_object(selected["return_receipt"])
        self.assertEqual(history_id, receipt["history_id"])
        self.assertEqual(returned["committed_revision"], receipt["project_revision"])
        self.assertEqual("Repair the test-only candidate.", selected["correction_reason"])
        self.assertEqual(candidate, self.json_object(selected["starting_snapshot"])["candidate"])
        reference = self.json_object(selected["starting_candidate"])
        self.assertEqual(
            reference["content_sha256"],
            hashlib.sha256((fixture.work / str(reference["selector"])).read_bytes()).hexdigest(),
        )
        add_unrelated_damaged_receipts(history_id)
        before_database = database.read_bytes()
        before_artifacts = {
            path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()
        }
        with (
            patch.object(
                SQLiteWorkStore, "validated_snapshot", side_effect=AssertionError("full validation is unrelated")
            ),
            patch.object(
                SQLiteWorkStore, "read_project_export_batches", side_effect=AssertionError("export is unrelated")
            ),
        ):
            grown = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
        self.assertEqual(baseline, grown)
        self.assertEqual(before_database, database.read_bytes())
        self.assertEqual(
            before_artifacts,
            {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()},
        )

    def test_final_source_read_failure_distinguishes_new_publication_from_unchanged_reuse(self) -> None:
        read = ArtifactRepository.read
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                fixture = self.correction_fixture()
                history_id, exact = self.submit_and_return(fixture, "returned", committed=True)
                current = self.current_candidate_choice(fixture, history_id, exact)
                if reuse:
                    self.assertEqual("ready", self.dispatch_native(fixture, current)["status"])
                brief_reads = 0

                def fail_selected_brief(
                    repository: ArtifactRepository, reference: stored_state.ArtifactReference
                ) -> bytes:
                    nonlocal brief_reads
                    if reference.kind == work_models.ArtifactKind.BRIEF:
                        brief_reads += 1
                        if brief_reads == 2:
                            raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "Selected brief cannot be reread.")
                    return read(repository, reference)

                before = fixture.store.validated_snapshot()
                with patch.object(ArtifactRepository, "read", new=fail_selected_brief):
                    result = fixture.call_advertised_tool(
                        server.DISPATCH_TOOL,
                        {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current},
                    )
                self.assertEqual("DISPATCH_REVIEW_READ_FAILED", result["code"], result)
                self.assertEqual("rejected" if reuse else "failed-after-publication", result["status"])
                self.assertEqual("unchanged" if reuse else "committed", result["effect"])
                self.assertEqual("correct-input" if reuse else "do-not-retry", result["retry"])
                after = fixture.store.validated_snapshot()
                self.assertEqual(before.lifecycle.work_items, after.lifecycle.work_items)
                self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
                if reuse:
                    self.assertEqual(before, after)

    def test_historical_read_survives_pause_and_new_protected_review_without_correction_authority(self) -> None:
        for state in ("paused", "review"):
            with self.subTest(state=state):
                fixture = self.correction_fixture()
                history_id, _choice = self.submit_and_return(fixture, "returned", committed=False)
                arguments: JsonObject = {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "attempt_id": "work-a-1",
                    "selection": {"kind": "historical", "return_history_id": history_id},
                }
                original = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
                match state:
                    case "paused":
                        paused = self.transition_result(
                            fixture,
                            self.project_action(fixture, "pause:work-a-1"),
                            {"reason": "Wait for the maintainer."},
                        )
                        self.assertEqual("committed", paused["status"], paused)
                    case "review":
                        (fixture.project / "tracked.txt").write_text("new protected review candidate\n")
                        lease = self.native_attempt_acquire(fixture, "new-review-worker")
                        action = self.native_actions(fixture, "submit-review", "work-a-1", role="worker", lease=lease)
                        submitted = self.transition_result(
                            fixture, action, {"candidate": root.read_working_tree_candidate(fixture.project).identity}
                        )
                        self.assertEqual("committed", submitted["status"], submitted)
                before = fixture.store.validated_snapshot()
                historical = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
                self.assertEqual(original, historical)
                if state == "review":
                    protected = fixture.store.read_candidate_snapshot_context(AttemptId("work-a-1"))
                    assert protected is not None
                    path = fixture.work / protected.reference.selector
                    protected_bytes = path.read_bytes()
                    path.write_bytes(b"unrelated newer protected snapshot damage\n")
                    with patch.object(
                        SQLiteWorkStore,
                        "read_review_job_context",
                        side_effect=AssertionError("current review context is unrelated"),
                    ):
                        selected = fixture.call_advertised_tool(server.RETURN_CONTEXT_TOOL, arguments)
                    self.assertEqual(original, selected)
                    path.write_bytes(protected_bytes)
                selection: JsonObject = {"kind": "current-candidate", "return_history_id": history_id}
                forbidden = fixture.call_advertised_tool(
                    server.RETURN_CONTEXT_TOOL, arguments | {"selection": selection}
                )
                self.assertEqual("DISPATCH_ATTEMPT_NOT_ACTIVE", forbidden["code"], forbidden)
                self.assertEqual(before, fixture.store.validated_snapshot())

    def test_current_candidate_read_rejects_noncanonical_omitted_names_before_result_or_publication(self) -> None:
        fixture = self.correction_fixture()
        history_id, _choice = self.submit_and_return(fixture, "returned", committed=True)
        (fixture.project / "scratch*.txt").write_text("omitted bytes\n")
        before = fixture.store.validated_snapshot()
        result = fixture.call_advertised_tool(
            server.RETURN_CONTEXT_TOOL,
            {
                "project_root": str(fixture.project),
                "work_root": str(fixture.work),
                "attempt_id": "work-a-1",
                "selection": {"kind": "current-candidate", "return_history_id": history_id},
            },
        )
        self.assertEqual("DISPATCH_CHECKOUT_MISMATCH", result["code"], result)
        self.assertFalse(result["state_changed"])
        self.assertEqual(before, fixture.store.validated_snapshot())
