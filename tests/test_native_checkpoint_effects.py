"""Checkpoint publication faults and races through current native transitions."""

import json
import os
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import UnexpectedToolError

from pinboard.adapters.files import artifacts as artifact_files
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import ArtifactError, ArtifactErrorCode, FileIOError, FileIOErrorCode
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, stored_state
from pinboard.application.artifacts import ArtifactPublication, NewArtifact
from pinboard.domain import work_models
from pinboard.mcp import server as mcp_server
from tests.checkpoint_support import CheckpointPackageSupport
from tests.support import JsonObject


class NativeCheckpointEffectsTest(CheckpointPackageSupport):
    def test_cross_boundary_unready_review_rejects_before_attempt_evidence_reads(self) -> None:
        for condition in ("missing", "malformed", "stale", "wrong-owner"):
            with self.subTest(condition=condition):
                fixture = self.checkpoint_fixture(review_condition=condition)
                action = self.project_action(fixture, "accept-checkpoint:work-a-1")
                payload = self.json_object(json.loads(fixture.payload.read_bytes()))
                before = fixture.store.validated_snapshot()
                files = {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
                original_read = Path.read_bytes

                def forbid_evidence(path: Path) -> bytes:
                    if path.name in {"result.md", "review.md"}:
                        raise AssertionError("Unready brief review reached implementation evidence reads.")
                    return original_read(path)  # noqa: B023 - used synchronously inside this subtest

                with patch.object(Path, "read_bytes", forbid_evidence):
                    failure = self.transition_result(fixture, action, payload)
                self.assertEqual(
                    ("rejected", "TRANSITION_INPUT_INVALID", False, []),
                    (failure["status"], failure["code"], failure["state_changed"], failure["changed_surfaces"]),
                )
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
                self.assertEqual(
                    files,
                    {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()},
                )

    def test_review_return_during_preflight_rejects_before_publication(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        before = fixture.store.validated_snapshot()
        original_facts = SQLiteWorkStore.read_decision_facts
        reads = 0

        def return_review(
            store: SQLiteWorkStore, scope: query_models.DecisionScope, observed_at: datetime
        ) -> query_models.DecisionFacts:
            nonlocal reads
            reads += 1
            facts = original_facts(store, scope, observed_at)
            if reads != 2:
                return facts
            return replace(
                facts,
                snapshot=replace(
                    facts.snapshot,
                    items=tuple(
                        replace(item, state=work_models.WorkState.ACTIVE) if str(item.item) == "work-a" else item
                        for item in facts.snapshot.items
                    ),
                    attempts=tuple(
                        replace(attempt, state=work_models.AttemptState.ACTIVE, protected_candidate_revision=None)
                        if str(attempt.attempt) == "work-a-1"
                        else attempt
                        for attempt in facts.snapshot.attempts
                    ),
                ),
            )

        with (
            patch.object(SQLiteWorkStore, "read_decision_facts", return_review),
            patch.object(ArtifactRepository, "publish", side_effect=AssertionError("Stale review published evidence.")),
        ):
            failure = self.transition_result(fixture, action, payload)
        self.assertEqual(
            ("rejected", "ACTION_NOT_AVAILABLE", False, []),
            (failure["status"], failure["code"], failure["state_changed"], failure["changed_surfaces"]),
        )
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_package_publication_failure_preserves_exact_three_prior_publications(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        before = fixture.store.validated_snapshot()
        publish = ArtifactRepository.publish

        def fail_package(repository: ArtifactRepository, artifact: NewArtifact) -> ArtifactPublication:
            if artifact.key.endswith("-review-package"):
                raise ArtifactError(ArtifactErrorCode.STORAGE_IO_ERROR, "controlled package publication failure")
            return publish(repository, artifact)

        with patch.object(ArtifactRepository, "publish", fail_package):
            failure = self.transition_result(fixture, action, payload)
        self.assertEqual(
            ("failed-after-publication", "STORAGE_IO_ERROR", "do-not-retry", True, ["immutable-artifact"]),
            (
                failure["status"],
                failure["code"],
                failure["retry"],
                failure["state_changed"],
                failure["changed_surfaces"],
            ),
        )
        selectors = tuple(str(self.json_object(value)["value"]) for value in self.json_array(failure["observed"]))
        checkpoint = fixture.brief.checkpoint.checkpoint_id
        self.assertEqual(
            (
                f"artifacts/evidence/work-a-1-{checkpoint}-candidate/1.json",
                f"artifacts/results/work-a-1-{checkpoint}-result/1.md",
                f"artifacts/evidence/work-a-1-{checkpoint}-review/1.md",
            ),
            selectors,
        )
        self.assertTrue(all((fixture.work / selector).is_file() for selector in selectors))
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_readonly_acceptance_preserves_publications_and_collision_recovery_uses_exact_bytes(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        before = fixture.store.validated_snapshot()
        error = StorageError(StorageErrorCode.READ_ONLY, "controlled readonly acceptance", retryable=False)
        with patch("pinboard.adapters.sqlite.state.append_history", side_effect=error):
            failure = self.transition_result(fixture, action, payload)
        self.assertEqual(
            ("failed-after-publication", "SQLITE_READONLY", "do-not-retry", True, ["immutable-artifact"]),
            (
                failure["status"],
                failure["code"],
                failure["retry"],
                failure["state_changed"],
                failure["changed_surfaces"],
            ),
        )
        selectors = tuple(str(self.json_object(value)["value"]) for value in self.json_array(failure["observed"]))
        self.assertEqual(4, len(selectors))
        published = {selector: (fixture.work / selector).read_bytes() for selector in selectors}
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        with self.assertRaises(UnexpectedToolError) as package_collision:
            self.transition_result(fixture, action, {**payload, "evidence": "Different evidence."})
        self.assertIsInstance(package_collision.exception.__cause__, ArtifactError)
        collision = package_collision.exception.__cause__
        assert isinstance(collision, ArtifactError)
        self.assertEqual(ArtifactErrorCode.STORAGE_INVARIANT_VIOLATION, collision.code)
        review_path = fixture.work / "attempts" / "work-a-1" / "review.md"
        review = review_path.read_bytes()
        review_path.write_bytes(b"conflicting review\n")
        with self.assertRaises(UnexpectedToolError) as evidence_collision:
            self.transition_result(fixture, action, payload)
        self.assertIsInstance(evidence_collision.exception.__cause__, ArtifactError)
        self.assertEqual(published, {selector: (fixture.work / selector).read_bytes() for selector in selectors})
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        review_path.write_bytes(review)
        accepted = self.transition_result(fixture, action, payload)
        self.assertEqual("committed", accepted["status"])
        self.assertEqual(["accepted-artifact-reference", "ledger"], accepted["changed_surfaces"])
        reloaded = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(len(before.transition_receipts) + 1, len(reloaded.transition_receipts))
        self.assertEqual(published, {selector: (fixture.work / selector).read_bytes() for selector in selectors})

    def test_selected_identity_and_preflight_failure_stop_before_evidence_and_publication(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        before = fixture.store.validated_snapshot()
        files = {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
        original_read = Path.read_bytes

        def reject_evidence(path: Path) -> bytes:
            if path.name in {"result.md", "review.md"}:
                raise AssertionError("Evidence was read before selected identity matched.")
            return original_read(path)

        with patch.object(Path, "read_bytes", reject_evidence):
            wrong_checkpoint = self.transition_result(fixture, action, {**payload, "checkpoint": "wrong-checkpoint"})
            wrong_candidate = self.transition_result(fixture, action, {**payload, "candidate": "wrong-candidate"})
        for failure in (wrong_checkpoint, wrong_candidate):
            self.assertEqual(
                ("rejected", "TRANSITION_INPUT_INVALID", False, []),
                (failure["status"], failure["code"], failure["state_changed"], failure["changed_surfaces"]),
            )
        stale = self.transition_result(fixture, {**action, "subject_revision": "stale"}, payload)
        self.assertEqual(
            ("rejected", "ACTION_NOT_AVAILABLE", False, []),
            (stale["status"], stale["code"], stale["state_changed"], stale["changed_surfaces"]),
        )
        error = StorageError(StorageErrorCode.IO_ERROR, "injected checkpoint preflight read failure", retryable=True)
        original_facts = SQLiteWorkStore.read_decision_facts
        reads = 0

        def fail_preflight(
            store: SQLiteWorkStore, scope: query_models.DecisionScope, observed_at: datetime
        ) -> query_models.DecisionFacts:
            nonlocal reads
            reads += 1
            if reads == 2:
                raise error
            return original_facts(store, scope, observed_at)

        with (
            patch.object(SQLiteWorkStore, "read_decision_facts", fail_preflight),
            self.assertRaises(UnexpectedToolError) as failure,
        ):
            self.transition_result(fixture, action, payload)
        self.assertEqual(2, reads)
        self.assertIs(error, failure.exception.__cause__)
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        self.assertEqual(
            files, {path: path.read_bytes() for path in (fixture.work / "artifacts").rglob("*") if path.is_file()}
        )

    def test_each_later_sync_failure_accounts_for_all_invocation_owned_publications(self) -> None:
        for failed_publication in (2, 3, 4):
            with self.subTest(failed_publication=failed_publication):
                fixture = self.checkpoint_fixture()
                action = self.project_action(fixture, "accept-checkpoint:work-a-1")
                payload = self.json_object(json.loads(fixture.payload.read_bytes()))
                before = fixture.store.validated_snapshot()
                checkpoint = fixture.brief.checkpoint.checkpoint_id
                selectors = (
                    f"artifacts/evidence/work-a-1-{checkpoint}-candidate/1.json",
                    f"artifacts/results/work-a-1-{checkpoint}-result/1.md",
                    f"artifacts/evidence/work-a-1-{checkpoint}-review/1.md",
                    f"artifacts/evidence/work-a-1-{checkpoint}-review-package/1.json",
                )
                create = artifact_files.create_immutable
                creations = 0

                def fail_sync(path: Path, content: bytes) -> bool:
                    nonlocal creations
                    creations += 1
                    if creations == failed_publication:  # noqa: B023 - used synchronously inside this subtest
                        with patch(
                            "pinboard.adapters.files.file_io._sync_directory",
                            side_effect=FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "controlled sync failure"),
                        ):
                            return create(path, content)  # noqa: B023 - used synchronously inside this subtest
                    return create(path, content)  # noqa: B023 - used synchronously inside this subtest

                with patch.object(artifact_files, "create_immutable", fail_sync):
                    failure = self.transition_result(fixture, action, payload)
                self.assertEqual(
                    ("failed-after-publication", "DIRECTORY_SYNC_FAILED", "do-not-retry", True, ["immutable-artifact"]),
                    (
                        failure["status"],
                        failure["code"],
                        failure["retry"],
                        failure["state_changed"],
                        failure["changed_surfaces"],
                    ),
                )
                self.assertEqual(
                    selectors[:failed_publication],
                    tuple(
                        str(self.json_object(value)["value"])
                        for value in self.json_array(failure["observed"])
                        if self.json_object(value)["field"] == "published_artifact_selector"
                    ),
                )
                for selector in selectors[:failed_publication]:
                    self.assertTrue((fixture.work / selector).is_file(follow_symlinks=False))
                self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())

    def test_post_link_sync_failure_preserves_exact_published_bytes_without_ledger_commit(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        before = fixture.store.validated_snapshot()
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        selectors = (
            f"artifacts/evidence/work-a-1-{checkpoint_id}-candidate/1.json",
            f"artifacts/results/work-a-1-{checkpoint_id}-result/1.md",
            f"artifacts/evidence/work-a-1-{checkpoint_id}-review/1.md",
        )
        result_bytes = (fixture.work / "attempts/work-a-1/result.md").read_bytes()
        review_bytes = (fixture.work / "attempts/work-a-1/review.md").read_bytes()
        review_publication = fixture.work / selectors[2]
        original_fsync = os.fsync

        def fail_after_review_link(descriptor: int) -> None:
            if review_publication.exists():
                raise OSError("injected post-link directory sync failure")
            original_fsync(descriptor)

        with patch("pinboard.adapters.files.file_io.os.fsync", side_effect=fail_after_review_link):
            failure = self.transition_result(fixture, action, payload)
        self.assertEqual("failed-after-publication", failure["status"])
        self.assertEqual("DIRECTORY_SYNC_FAILED", failure["code"])
        self.assertEqual("do-not-retry", failure["retry"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual(
            selectors,
            tuple(
                str(self.json_object(value)["value"])
                for value in self.json_array(failure["observed"])
                if self.json_object(value)["field"] == "published_artifact_selector"
            ),
        )
        self.assertEqual(before, SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        self.assertEqual(result_bytes, (fixture.work / selectors[1]).read_bytes())
        self.assertEqual(review_bytes, review_publication.read_bytes())
        # A caller may inspect and deliberately select the still-current receipt;
        # the failed response itself never advertises an automatic replay.
        fresh = self.project_action(fixture, "accept-checkpoint:work-a-1")
        completed = self.transition_result(fixture, fresh, payload)
        self.assertEqual("committed", completed["status"])

    def test_concurrent_loser_does_not_claim_winner_publication_as_its_own_effect(self) -> None:
        fixture = self.checkpoint_fixture()
        action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        request = self.native_transition_request(fixture, action, payload)
        before = fixture.store.validated_snapshot()
        loser_reached_creation = threading.Event()
        resume_loser = threading.Event()
        loser_results: list[JsonObject] = []
        loser_errors: list[BaseException] = []
        absence_observations: list[bool] = []
        original_create = artifact_files.create_immutable

        def coordinate_creation(path: Path, content: bytes) -> bool:
            if threading.current_thread() is loser and not loser_reached_creation.is_set():
                absence_observations.append(not path.exists())
                loser_reached_creation.set()
                if not resume_loser.wait(timeout=5):
                    raise AssertionError("Checkpoint loser did not resume.")
            return original_create(path, content)

        def run_loser() -> None:
            try:
                # Direct handler execution keeps the deterministic effect boundary
                # on this named request thread; winner exercises the registered SDK.
                loser_results.append(mcp_server._transition(request, mcp_server.CancellationToken()).content)
            except BaseException as error:  # pragma: no cover - asserted by coordinator
                loser_errors.append(error)

        loser = threading.Thread(target=run_loser)
        with patch("pinboard.adapters.files.artifacts.create_immutable", side_effect=coordinate_creation):
            loser.start()
            try:
                self.assertTrue(loser_reached_creation.wait(timeout=5))
                winner = self.transition_result(fixture, action, payload)
            finally:
                resume_loser.set()
            loser.join(timeout=5)
        self.assertFalse(loser.is_alive())
        self.assertEqual([], loser_errors)
        self.assertEqual([True], absence_observations)
        self.assertEqual("committed", winner["status"])
        self.assertEqual(1, len(loser_results))
        rejected = loser_results[0]
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("ACTION_NOT_AVAILABLE", rejected["code"])
        self.assertFalse(rejected["state_changed"])
        self.assertEqual([], rejected["changed_surfaces"])
        self.assertFalse(
            any(
                self.json_object(value)["field"] == "published_artifact_selector"
                for value in self.json_array(rejected["observed"])
            )
        )
        after = SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.artifact_references) + 4, len(after.artifact_references))
        self.assertEqual(len(before.transition_receipts) + 1, len(after.transition_receipts))

    def test_replacement_recorded_after_publication_blocks_locked_checkpoint_acceptance(self) -> None:
        fixture = self.checkpoint_fixture()
        checkpoint_action = self.project_action(fixture, "accept-checkpoint:work-a-1")
        replacement_action = self.project_action(fixture, "record-replacement:work-a")
        payload = self.json_object(json.loads(fixture.payload.read_bytes()))
        relation: JsonObject = {
            "schema": "pinboard-planned-replacement/v1",
            "affected_item": "work-a",
            "expected_relation_revision": 0,
            "replacement_item": "work-c",
            "replacement_cost": "Checkpoint acceptance would preserve replaced work.",
            "status": "current",
            "recorded_by": "review-owner",
        }
        original_publish = ArtifactRepository.publish
        relation_state: list[stored_state.StoredWorkState] = []

        def publish_then_record(repository: ArtifactRepository, artifact: NewArtifact) -> ArtifactPublication:
            publication = original_publish(repository, artifact)
            if artifact.key.endswith("-review-package") and not relation_state:
                recorded = self.transition_result(fixture, replacement_action, relation)
                self.assertEqual("committed", recorded["status"])
                relation_state.append(SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
            return publication

        with patch.object(ArtifactRepository, "publish", publish_then_record):
            failure = self.transition_result(fixture, checkpoint_action, payload)
        self.assertEqual("failed-after-publication", failure["status"])
        self.assertEqual("ACTION_NOT_AVAILABLE", failure["code"])
        self.assertTrue(failure["state_changed"])
        self.assertEqual(["immutable-artifact"], failure["changed_surfaces"])
        self.assertEqual("do-not-retry", failure["retry"])
        checkpoint_id = fixture.brief.checkpoint.checkpoint_id
        self.assertEqual(
            (
                f"artifacts/evidence/work-a-1-{checkpoint_id}-candidate/1.json",
                f"artifacts/results/work-a-1-{checkpoint_id}-result/1.md",
                f"artifacts/evidence/work-a-1-{checkpoint_id}-review/1.md",
                f"artifacts/evidence/work-a-1-{checkpoint_id}-review-package/1.json",
            ),
            tuple(
                str(self.json_object(value)["value"])
                for value in self.json_array(failure["observed"])
                if self.json_object(value)["field"] == "published_artifact_selector"
            ),
        )
        self.assertEqual(1, len(relation_state))
        self.assertEqual(relation_state[0], SQLiteWorkStore(fixture.work / "state.sqlite3").validated_snapshot())
        after = relation_state[0]
        self.assertTrue(
            any(str(value.replacement_item_id) == "work-c" for value in after.replacements.planned_replacements)
        )
