import dataclasses
import hashlib
import json
import subprocess
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, tzinfo
from itertools import count
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters import dispatch_operations
from pinboard.adapters.files import root
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import (
    action_models,
    candidate_snapshots,
    query_models,
    stored_state,
    work_brief_models,
    work_briefs,
)
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.application.dispatch_models import DispatchArtifactPort
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, HistoryId
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import job_operations as mcp_jobs
from pinboard.mcp import server
from tests import test_dispatch
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.native_support import call_native_tool
from tests.support import SQLITE_NOW, JsonObject
from tests.work_brief_support import ready_review, work_a_brief


class CorrectionSourceReviewTest(CheckpointPackageSupport):
    def correction_fixture(self) -> CheckpointFixture:
        fixture = self.checkpoint_fixture()
        payload = fixture.work / "bootstrap-return.json"
        payload.write_text('{"reason":"Prepare the real correction scenario."}', encoding="utf-8")
        self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
        (fixture.project / "tracked.txt").write_text("base\n", encoding="utf-8")
        tests = fixture.project / "tests"
        tests.mkdir()
        (tests / "test_only.py").write_text("assert True\n", encoding="utf-8")
        self.commit_all(fixture.project, "test baseline")
        return fixture

    def submit_and_return(self, fixture: CheckpointFixture, label: str, *, committed: bool) -> tuple[int, JsonObject]:
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
        payload.write_text('{"reason":"Repair the test-only candidate."}', encoding="utf-8")
        receipt = self.transition_json(fixture, self.project_action(fixture, "return-for-correction:work-a-1"), payload)
        history_id = receipt["history_id"]
        assert isinstance(history_id, int)
        return history_id, self.correction_choice(
            fixture, label, reference, history_id, "Repair the test-only candidate."
        )

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

    def test_replacement_brief_uses_historical_findings_but_requires_a_new_current_return(self) -> None:
        with (
            patch("pinboard.mcp.job_operations.datetime", wraps=datetime) as boundary_clock,
            patch("tests.checkpoint_support.datetime", wraps=datetime) as fixture_clock,
        ):
            clock_ticks = count()

            def sampled_time(_: tzinfo) -> datetime:
                return SQLITE_NOW + timedelta(seconds=next(clock_ticks))

            boundary_clock.now.side_effect = sampled_time
            fixture_clock.now.return_value = SQLITE_NOW
            for publish_ready in (False, True):
                with self.subTest(publish_ready=publish_ready):
                    self.replacement_brief_recovery(publish_ready)

    def replacement_brief_recovery(self, publish_ready: bool) -> None:  # noqa: PLR0915 - one complete causal recovery journey
        fixture = self.correction_fixture()
        old_history, old_choice = self.submit_and_return(fixture, "returned-test", committed=True)
        candidate = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture.project, check=True, capture_output=True, text=True
        ).stdout.strip()
        replacement = msgspec.structs.replace(
            fixture.brief,
            artifact_revision=fixture.brief.artifact_revision + 1,
            checkpoint=msgspec.structs.replace(
                fixture.brief.checkpoint, title="Review a genuinely distinct replacement checkpoint"
            ),
            bootstrap=(
                *fixture.brief.bootstrap,
                "Reassess the protected candidate against the complete replacement brief.",
            ),
        )
        published = call_native_tool(
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
                "attempt": "work-a-1",
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
        stale = call_native_tool(
            server.DISPATCH_TOOL,
            {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": old_choice},
        )
        self.assertEqual("DISPATCH_BRIEF_REVIEW_ARGUMENT_INVALID", stale["code"], stale)
        self.assertFalse(stale["state_changed"])
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

        if publish_ready:
            environment = msgspec.structs.replace(
                test_dispatch.DispatchTest().environment(fixture.project),
                branch=replacement.branch,
                starting_revision=replacement.base_revision,
            )
            published_ready = call_native_tool(
                server.DISPATCH_TOOL,
                {
                    "project_root": str(fixture.project),
                    "work_root": str(fixture.work),
                    "dispatch": {
                        "kind": "reviewed",
                        "receipt": {
                            "action_id": {"kind": "dispatch", "subject": "work-a-1"},
                            "subject_revision": action["subject_revision"],
                        },
                        "checkpoint_id": replacement.checkpoint.checkpoint_id,
                        "environment": msgspec.to_builtins(
                            environment, enc_hook=test_dispatch.dispatch_environment_enc_hook
                        ),
                        "prompt": None,
                        "brief_review": msgspec.json.decode(ready_review(replacement)),
                        "review_id": "replacement-readiness",
                    },
                },
            )
            self.assertEqual("ready", published_ready["status"], published_ready)
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
        reason = "Apply the complete replacement-brief review, retaining the prior finding."
        returned = self.transition_result(
            fixture, self.project_action(fixture, "return-for-correction:work-a-1"), {"reason": reason}
        )
        self.assertEqual("committed", returned["status"], returned)
        new_history = returned["history_id"]
        assert isinstance(new_history, int)
        self.assertNotEqual(old_history, new_history)
        current_choice = self.correction_choice(
            fixture, "replacement-correction", context.reference, new_history, reason
        )
        self.assertEqual(
            "ready",
            call_native_tool(
                server.DISPATCH_TOOL,
                {"project_root": str(fixture.project), "work_root": str(fixture.work), "dispatch": current_choice},
            )["status"],
        )
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
            work_models.ArtifactKind.EVIDENCE, f"work-a-1-{replacement.checkpoint.checkpoint_id}-review-package", 1
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
