import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters import dispatch_operations
from pinboard.adapters.files import root
from pinboard.adapters.files.brief_sources import select_checkout_brief_source
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import action_models, candidate_snapshots, work_brief_models, work_briefs
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.application.dispatch_models import DispatchArtifactPort
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId
from pinboard.mcp import server
from tests import test_dispatch
from tests.checkpoint_support import CheckpointFixture, CheckpointPackageSupport
from tests.support import JsonObject
from tests.work_brief_support import ready_review, work_a_brief


class CorrectionSourceReviewTest(CheckpointPackageSupport):
    def correction_fixture(self) -> CheckpointFixture:
        fixture = self.checkpoint_fixture()
        payload = fixture.work / "bootstrap-return.json"
        payload.write_text('{"reason":"Prepare the real correction scenario."}', encoding="utf-8")
        self.transition_json(fixture, self.project_action(fixture.common, "return-for-correction:work-a-1"), payload)
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
        lease = self.run_json_cli(
            *fixture.common,
            "attempt",
            "acquire",
            "--attempt-id",
            "work-a-1",
            "--task-id",
            label,
            "--host-id",
            "local",
            "--ttl-seconds",
            "300",
        )
        actions = self.run_json_cli(
            *fixture.common,
            "actions",
            "--role",
            "worker",
            "--lease-id",
            str(lease["lease_id"]),
            "--generation",
            str(lease["generation"]),
            "--action-id",
            "submit-review:work-a-1",
        )
        payload = fixture.work / "submit.json"
        payload.write_text(json.dumps({"candidate": candidate}), encoding="utf-8")
        self.transition_json(fixture, self.json_object(self.json_array(actions["actions"])[0]), payload)
        context = SQLiteWorkStore(fixture.work / "state.sqlite3").read_candidate_snapshot_context(AttemptId("work-a-1"))
        assert context is not None
        reference = context.reference
        payload.write_text('{"reason":"Repair the test-only candidate."}', encoding="utf-8")
        receipt = self.transition_json(
            fixture, self.project_action(fixture.common, "return-for-correction:work-a-1"), payload
        )
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
            action_models.ReasonInputPayload("Repair the test-only candidate."),
            "The complete accepted starting snapshot includes the changed test; the proposed repair preserves the reviewed contracts.",
        )
        action = self.project_action(fixture.common, "dispatch:work-a-1")
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
                "correction_history_id": receipt["history_id"],
            },
            enc_hook=test_dispatch.dispatch_environment_enc_hook,
        )
        assert isinstance(choice, dict)
        history_id = receipt["history_id"]
        assert isinstance(history_id, int)
        return history_id, choice

    def dispatch_cli(self, fixture: CheckpointFixture, choice: JsonObject) -> JsonObject:
        environment = fixture.work / "environment.json"
        review = fixture.work / "correction-review.json"
        environment.write_bytes(msgspec.json.encode(choice["environment"]))
        review.write_bytes(msgspec.json.encode(choice["brief_review"]))
        receipt = self.json_object(choice["receipt"])
        result, stdout, stderr = self.run_cli(
            *fixture.common,
            "dispatch",
            "--action-id",
            "dispatch:work-a-1",
            "--subject-revision",
            str(receipt["subject_revision"]),
            "--task-id",
            "review-owner",
            "--host-id",
            "local",
            "--checkpoint",
            str(choice["checkpoint_id"]),
            "--environment",
            str(environment),
            "--brief-review",
            str(review),
            "--review-id",
            str(choice["review_id"]),
            "--correction-history-id",
            str(choice["correction_history_id"]),
            "--json",
        )
        self.assertIn(result, (0, 14), stderr)
        return self.json_object(json.loads(stdout))

    def test_real_cli_mcp_two_test_only_candidates_reload_distinct_subjects_and_initial_proof(self) -> None:
        fixture = self.correction_fixture()
        checkpoint = fixture.brief.checkpoint
        initial_key = (
            f"work-a-1-brief-review-{hashlib.sha256(work_briefs.canonical_checkpoint_bytes(checkpoint)).hexdigest()}"
        )
        initial_reference = fixture.store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, initial_key, 1)
        assert initial_reference is not None
        initial_bytes = (fixture.work / initial_reference.selector).read_bytes()
        _, first = self.submit_and_return(fixture, "first-test", committed=False)
        self.assertEqual("ready", self.dispatch_cli(fixture, first)["status"])
        first_state = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        first_reviews = {value.key for value in first_state.artifact_references if "-brief-review-" in value.key}
        replay = server._dispatch_job(str(fixture.project), str(fixture.work), first, server.CancellationToken())
        self.assertEqual("unchanged", replay.content["effect"], replay.content)
        _, second = self.submit_and_return(fixture, "second-test", committed=False)
        self.assertEqual(
            self.json_object(first["brief_review"])["contract_review"],
            self.json_object(second["brief_review"])["contract_review"],
        )
        ready = server._dispatch_job(str(fixture.project), str(fixture.work), second, server.CancellationToken())
        self.assertEqual("ready", ready.content["status"], ready.content)
        second_state = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        second_reviews = {value.key for value in second_state.artifact_references if "-brief-review-" in value.key}
        self.assertEqual(1, len(second_reviews - first_reviews))
        self.assertEqual(initial_bytes, (fixture.work / initial_reference.selector).read_bytes())
        collision = deepcopy(second)
        self.json_object(collision["brief_review"])["assessment"] = (
            "Different evidence about the actually identical complete subject."
        )
        collision["review_id"] = "different-proof"
        outcome = server._dispatch_job(str(fixture.project), str(fixture.work), collision, server.CancellationToken())
        self.assertEqual("DISPATCH_BRIEF_REVIEW_COLLISION", outcome.content["code"])
        self.assertEqual("failed-after-publication", outcome.content["status"])
        self.assertEqual("do-not-retry", outcome.content["retry"])
        replay_collision = server._dispatch_job(
            str(fixture.project), str(fixture.work), collision, server.CancellationToken()
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
                outcome = self.dispatch_cli(fixture, invalid)
                self.assertNotEqual("ready", outcome["status"], outcome)
                self.assertFalse(outcome["state_changed"])
                self.assertEqual(before, fixture.store.validated_snapshot())
        self.commit_all(fixture.project, "different preimage")
        before = fixture.store.validated_snapshot()
        rejected = server._dispatch_job(str(fixture.project), str(fixture.work), choice, server.CancellationToken())
        self.assertEqual("DISPATCH_BRIEF_REVIEW_STALE", rejected.content["code"])
        self.assertEqual(before, fixture.store.validated_snapshot())

    def test_dirty_commit_and_late_checkout_change_preserve_truthful_effects(self) -> None:
        fixture = self.correction_fixture()
        _, choice = self.submit_and_return(fixture, "committed-test", committed=True)
        test_file = fixture.project / "tests" / "test_only.py"
        exact = test_file.read_bytes()
        test_file.write_bytes(b"assert 'dirty'\n")
        before = fixture.store.validated_snapshot()
        rejected = self.dispatch_cli(fixture, choice)
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
            outcome = server._dispatch_job(str(fixture.project), str(fixture.work), choice, server.CancellationToken())
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
            correction = {
                "schema": "pinboard-correction-source-review/v1",
                "contract_review": msgspec.json.decode(initial),
                "starting_candidate": {
                    "role": "candidate",
                    "kind": "evidence",
                    "key": "snapshot",
                    "revision": 1,
                    "selector": "artifacts/evidence/snapshot/1.json",
                    "content_sha256": "a" * 64,
                    "size_bytes": 10,
                },
                "correction_input": {"reason": "Repair the actual starting candidate."},
                "assessment": "The exact snapshot and proposed correction agree with the contract review.",
            }
            decoded = work_briefs.decode_correction_source_review(msgspec.json.encode(correction))
            self.assertIsInstance(decoded, work_brief_models.CorrectionSourceReview)
            self.assertIsInstance(
                work_briefs.decode_correction_source_review(initial), work_brief_models.WorkBriefFailure
            )
            changes: tuple[dict[str, work_brief_models.WorkBriefJsonValue], ...] = (
                {"unknown": True},
                {"starting_candidate": {}},
                {"correction_input": {"reason": ""}},
            )
            for change in changes:
                with self.subTest(change=change):
                    self.assertIsInstance(
                        work_briefs.decode_correction_source_review(msgspec.json.encode(correction | change)),
                        work_brief_models.WorkBriefFailure,
                    )
