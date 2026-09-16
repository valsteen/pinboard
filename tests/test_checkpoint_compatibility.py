"""Retained checkpoint v1 recovery and its committed-effect contract."""

import hashlib
import json
import sqlite3
from unittest.mock import patch

from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import candidate_snapshots, checkpoint_compatibility_models, work_brief_models
from pinboard.cli import work_inspection
from pinboard.domain import work_models
from tests.checkpoint_support import AcceptedPackageFixture, CheckpointPackageSupport


class CheckpointCompatibilityTest(CheckpointPackageSupport):
    def compatibility_review_fixture(self) -> tuple[AcceptedPackageFixture, int, int, bytes]:
        fixture, history_id, correction_history_id = self.review_job_fixture()
        package = self.package(fixture)
        assert isinstance(package, work_brief_models.CheckpointReviewPackageV3)
        candidate_reference = next(
            value
            for value in fixture.store.validated_snapshot().artifact_references
            if value.key == package.candidate_snapshot.key
        )
        candidate_bytes = candidate_snapshots.decode_candidate_snapshot(
            (fixture.work / candidate_reference.selector).read_bytes()
        ).diff
        historical_candidate = f"working-tree-sha256:{hashlib.sha256(candidate_bytes).hexdigest()}"
        legacy = checkpoint_compatibility_models.CheckpointReviewPackage(
            package.attempt_id,
            package.item_id,
            historical_candidate,
            package.acceptance_evidence,
            package.accepted_scope,
            package.checkpoint,
            package.accepted_brief,
            package.result,
            package.implementation_review,
            package.verdict,
            package.review_basis,
        )
        self.replace_package(fixture, legacy)
        connection = sqlite3.connect(fixture.work / "state.sqlite3")
        try:
            connection.execute(
                "UPDATE transition_history SET outcome_json = json_set(outcome_json, '$.candidate', ?) WHERE history_id = ?",
                (historical_candidate, history_id),
            )
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_ref_id = ?", (int(candidate_reference.artifact_ref_id),)
            )
            connection.commit()
        finally:
            connection.close()
        (fixture.work / candidate_reference.selector).unlink()
        return fixture, history_id, correction_history_id, candidate_bytes

    def test_legacy_review_missing_candidate_is_actionable_and_exact_recovery_continues(self) -> None:
        fixture, history_id, correction_history_id, candidate_bytes = self.compatibility_review_fixture()
        common = (
            *fixture.common,
            "review-job",
            "--attempt-id",
            "work-a-1",
            "--candidate-revision",
            "b" * 40,
            "--checkpoint-history-id",
            str(history_id),
        )
        before = fixture.store.validated_snapshot()

        result, stdout, stderr = self.run_cli(*common, "--json")

        self.assertEqual(11, result, stderr)
        rejected = self.json_object(json.loads(stdout))
        self.assertEqual("correct-input", rejected["retry"])
        self.assertFalse(rejected["state_changed"])
        self.assertEqual([], rejected["changed_surfaces"])
        observed_rows = rejected["observed"]
        if not isinstance(observed_rows, list):
            self.fail("Expected structured observations")
        observed = {
            str(row["field"]): row["value"] for value in observed_rows if isinstance(value, dict) for row in (value,)
        }
        self.assertEqual(history_id, observed["checkpoint_history_id"])
        self.assertIn("--candidate-patch", str(observed["recovery_command"]))
        self.assertEqual(before, fixture.store.validated_snapshot())

        wrong = fixture.project / "wrong.patch"
        wrong.write_bytes(b"wrong\n")
        wrong_result, wrong_stdout, wrong_stderr = self.run_cli(*common, "--candidate-patch", str(wrong), "--json")
        self.assertEqual(11, wrong_result, wrong_stderr)
        wrong_rejection = self.json_object(json.loads(wrong_stdout))
        self.assertEqual("correct-input", wrong_rejection["retry"])
        self.assertFalse(wrong_rejection["state_changed"])
        self.assertEqual([], wrong_rejection["changed_surfaces"])

        recovered = fixture.project / "candidate.patch"
        recovered.write_bytes(candidate_bytes)
        job = self.run_json_cli(*common, "--candidate-patch", str(recovered))
        self.assertEqual("pinboard-review-job/v4", job["schema"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            job["changed_surfaces"],
        )
        selected = self.json_object(job["prior_checkpoint_package"])
        self.assertEqual(hashlib.sha256(candidate_bytes).hexdigest(), selected["candidate_sha256"])
        accepted = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-candidate",
            1,
        )
        self.assertIsNotNone(accepted)
        after_recovery = fixture.store.validated_snapshot()

        repeated = self.run_json_cli(*common, "--candidate-patch", str(recovered))
        self.assertEqual([], repeated["changed_surfaces"])
        self.assertEqual(after_recovery, fixture.store.validated_snapshot())

        correction = self.run_json_cli(
            *common,
            "--correction-history-id",
            str(correction_history_id),
            "--candidate-patch",
            str(recovered),
        )
        self.assertEqual("correction", self.json_object(correction["review_round"])["kind"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            correction["changed_surfaces"],
        )

    def test_legacy_recovery_reports_committed_effect_when_later_review_input_fails(self) -> None:
        fixture, history_id, _correction_history_id, candidate_bytes = self.compatibility_review_fixture()
        (fixture.work / "attempts" / "work-a-1" / "result.md").unlink()
        recovered = fixture.project / "candidate.patch"
        recovered.write_bytes(candidate_bytes)

        result, stdout, stderr = self.run_cli(
            *fixture.common,
            "review-job",
            "--attempt-id",
            "work-a-1",
            "--candidate-revision",
            "b" * 40,
            "--checkpoint-history-id",
            str(history_id),
            "--candidate-patch",
            str(recovered),
            "--json",
        )

        self.assertEqual(11, result, stderr)
        rejected = self.json_object(json.loads(stdout))
        self.assertEqual("committed-effect", rejected["status"])
        self.assertEqual("do-not-retry", rejected["retry"])
        self.assertEqual(
            ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            rejected["changed_surfaces"],
        )
        accepted = fixture.store.read_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            f"work-a-1-{fixture.brief.checkpoint.checkpoint_id}-candidate",
            1,
        )
        self.assertIsNotNone(accepted)

    def test_legacy_recovery_reports_committed_effect_for_later_exceptions(self) -> None:
        for boundary in ("context-reread", "prompt-publication"):
            with self.subTest(boundary=boundary):
                fixture, history_id, _correction_history_id, candidate_bytes = self.compatibility_review_fixture()
                recovered = fixture.project / "candidate.patch"
                recovered.write_bytes(candidate_bytes)
                failure = StorageError(StorageErrorCode.IO_ERROR, f"{boundary} failed")
                arguments = (
                    *fixture.common,
                    "review-job",
                    "--attempt-id",
                    "work-a-1",
                    "--candidate-revision",
                    "b" * 40,
                    "--checkpoint-history-id",
                    str(history_id),
                    "--candidate-patch",
                    str(recovered),
                    "--json",
                )

                if boundary == "context-reread":
                    original = work_inspection.queries.select_review_job_context
                    calls = 0

                    def fail_second_read(
                        *args: object,
                        original_select: object = original,
                        selected_failure: StorageError = failure,
                        **kwargs: object,
                    ) -> object:
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise selected_failure
                        assert callable(original_select)
                        return original_select(*args, **kwargs)

                    selected_patch = patch.object(
                        work_inspection.queries,
                        "select_review_job_context",
                        side_effect=fail_second_read,
                    )
                else:
                    selected_patch = patch.object(
                        work_inspection.review_operations.dispatch_models,
                        "publish_agent_prompt",
                        side_effect=failure,
                    )

                before = fixture.store.validated_snapshot()
                with selected_patch:
                    result, stdout, stderr = self.run_cli(*arguments)

                self.assertEqual(12, result, stderr)
                rejected = self.json_object(json.loads(stdout))
                self.assertEqual("committed-effect", rejected["status"])
                self.assertEqual("do-not-retry", rejected["retry"])
                self.assertEqual(
                    ["immutable-artifact", "accepted-artifact-reference", "ledger"],
                    rejected["changed_surfaces"],
                )
                observed_rows = rejected["observed"]
                if not isinstance(observed_rows, list):
                    self.fail("Expected structured observations")
                observed = {
                    str(row["field"]): row["value"]
                    for value in observed_rows
                    if isinstance(value, dict)
                    for row in (value,)
                }
                self.assertIn("candidate", str(observed["published_artifact_selector"]))
                after = fixture.store.validated_snapshot()
                self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
