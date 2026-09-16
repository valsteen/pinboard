import hashlib
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import msgspec

from pinboard.adapters import lifecycle_artifacts
from pinboard.adapters.files import candidate_compatibility
from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, RootError, RootErrorCode
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.root import (
    CandidateRestoreAfterMutationError,
    CandidateRestoreRejection,
    CandidateRestoreSuccess,
    CurrentHeadCandidate,
    DifferentHeadCandidate,
    DirtyHeadCandidate,
    WorkingTreeCandidate,
    read_working_tree_candidate,
    restore_commit_candidate,
    restore_working_tree_candidate,
)
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import candidate_snapshot_compatibility_models, query_models
from pinboard.application.artifacts import ArtifactPublication, ArtifactRef
from pinboard.application.candidate_identity import working_tree_identity
from pinboard.application.candidate_snapshots import (
    CandidateSnapshotEvidence,
    CandidateSnapshotReceiptInput,
    CommitCandidateSnapshot,
    WorkingTreeCandidateSnapshot,
    candidate_snapshot_key,
    canonical_candidate_snapshot_bytes,
    decode_candidate_snapshot,
    legacy_review_candidate,
    validate_candidate_snapshot_history,
    verify_candidate_snapshot_context,
)
from pinboard.cli import candidate_recovery, cli_commands
from pinboard.cli.errors import CommandFailure, CommittedEffectFailure
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import (
    ArtifactAcceptanceAfterPublicationError,
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    EffectDisposition,
)
from pinboard.domain.identifiers import ActionId, AttemptId, CandidateId
from tests.domain_support import action
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store


class CandidateSnapshotTest(unittest.TestCase):
    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()

    def repository(self) -> tuple[Path, str]:
        root = Path(tempfile.mkdtemp()).resolve()
        self.git(root, "init", "-b", "main")
        (root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(root, "add", "tracked.txt")
        self.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        return root, self.git(root, "rev-parse", "HEAD")

    def initialized_store(self) -> SQLiteWorkStore:
        root = Path(tempfile.mkdtemp()).resolve()
        durable = resolve_durable_roots(root)
        initialize_database(durable, SQLITE_NOW)
        store = SQLiteWorkStore(durable.database_path)
        initialize_store(store, complete_sqlite_state())
        return store

    def clone(self, source: Path) -> Path:
        target = Path(tempfile.mkdtemp()).resolve()
        self.git(target.parent, "clone", "--quiet", str(source), str(target))
        return target

    def rejection(self, result: CandidateRestoreSuccess | CandidateRestoreRejection) -> CandidateRestoreRejection:
        if not isinstance(result, CandidateRestoreRejection):
            self.fail(f"Expected candidate restore rejection, received {result!r}")
        return result

    def snapshot_context(
        self,
    ) -> tuple[
        WorkingTreeCandidateSnapshot,
        query_models.CandidateSnapshotContextFacts,
        bytes,
    ]:
        state = complete_sqlite_state()
        attempt = state.lifecycle.attempts[0]
        preimage = "a" * 40
        candidate = working_tree_identity(preimage, b"")
        snapshot = WorkingTreeCandidateSnapshot(
            "pinboard-candidate-snapshot/v2",
            str(attempt.attempt_id),
            str(attempt.item_id),
            candidate,
            attempt.branch,
            preimage,
            attempt.base_revision,
            SQLITE_NOW.isoformat(),
            b"",
        )
        encoded = canonical_candidate_snapshot_bytes(snapshot)
        reference = replace(
            state.artifact_references[2],
            key=candidate_snapshot_key(snapshot),
            selector="artifacts/evidence/candidate/1.json",
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
        )
        receipt_input = CandidateSnapshotReceiptInput(candidate, int(reference.artifact_ref_id))
        receipt = replace(
            state.transition_receipts[0],
            action_id=ActionId("submit-review:work-a-1"),
            action_kind=decision_models.ActionKind.SUBMIT_REVIEW,
            artifact_ref_id=reference.artifact_ref_id,
            input_schema="pinboard-candidate-snapshot/v2",
            input_payload=work_models.CanonicalJson(msgspec.json.encode(receipt_input, order="sorted")),
            outcome_schema="transition-receipt/v1",
            outcome_payload=work_models.CanonicalJson(
                history.encode_transition_receipt_outcome(
                    evidence=None,
                    outcome="submit-review",
                    candidate=candidate,
                )
            ),
        )
        context = query_models.CandidateSnapshotContextFacts(
            attempt.attempt_id,
            attempt.item_id,
            attempt.state,
            attempt.branch,
            attempt.base_revision,
            None,
            None,
            receipt,
            reference,
        )
        return snapshot, context, encoded

    def test_current_working_tree_snapshot_requires_full_actual_preimage_vocabulary(self) -> None:
        snapshot, _context, _encoded = self.snapshot_context()
        short_preimage = msgspec.structs.replace(
            snapshot,
            preimage_revision="short",
            candidate=working_tree_identity("short", snapshot.diff),
        )
        with self.assertRaises(msgspec.DecodeError):
            decode_candidate_snapshot(canonical_candidate_snapshot_bytes(short_preimage))

    def test_legacy_review_receipts_remain_valid_with_exact_live_correlation(self) -> None:
        state = complete_sqlite_state()
        candidate = "working-tree-sha256:" + "0" * 64
        legacy_receipt = replace(
            state.transition_receipts[0],
            action_id=ActionId("submit-review:work-a-1"),
            action_kind=decision_models.ActionKind.SUBMIT_REVIEW,
            artifact_ref_id=None,
            input_schema="decision/v1",
            input_payload=work_models.CanonicalJson(b"{}"),
            outcome_schema="transition-receipt/v1",
            outcome_payload=work_models.CanonicalJson(
                history.encode_transition_receipt_outcome(
                    evidence=None,
                    outcome="submit-review",
                    candidate=candidate,
                )
            ),
        )
        historical = replace(state, transition_receipts=(legacy_receipt,))

        self.assertEqual((), validate_candidate_snapshot_history(historical, {}))

        attempt = historical.lifecycle.attempts[0]
        live_attempt = replace(
            attempt,
            state=work_models.AttemptState.REVIEW,
            candidate_revision=candidate,
            candidate_recorded_at=SQLITE_NOW,
        )
        live = replace(
            historical,
            lifecycle=replace(historical.lifecycle, attempts=(live_attempt,)),
        )
        self.assertEqual((), validate_candidate_snapshot_history(live, {}))

        mismatched = replace(
            live,
            lifecycle=replace(
                live.lifecycle,
                attempts=(replace(live_attempt, candidate_revision="working-tree-sha256:" + "1" * 64),),
            ),
        )
        with self.assertRaisesRegex(ValueError, "live review attempt lacks"):
            validate_candidate_snapshot_history(mismatched, {})

    def test_legacy_review_receipt_requires_exact_canonical_outcome(self) -> None:
        state = complete_sqlite_state()
        candidate = "working-tree-sha256:" + "0" * 64
        receipt = replace(
            state.transition_receipts[0],
            action_kind=decision_models.ActionKind.SUBMIT_REVIEW,
            artifact_ref_id=None,
            input_schema="decision/v1",
            input_payload=work_models.CanonicalJson(b"{}"),
            outcome_schema="transition-receipt/v1",
            outcome_payload=work_models.CanonicalJson(
                history.encode_transition_receipt_outcome(
                    evidence=None,
                    outcome="submit-review",
                    candidate=candidate,
                )
            ),
        )
        self.assertIsNone(legacy_review_candidate(state.transition_receipts[0]))
        self.assertEqual(candidate, legacy_review_candidate(receipt))
        invalid = (
            replace(receipt, input_payload=work_models.CanonicalJson(b'{"extra":true}')),
            replace(receipt, outcome_schema="wrong/v1"),
            replace(receipt, outcome_payload=work_models.CanonicalJson(bytes(receipt.outcome_payload) + b" ")),
            replace(
                receipt,
                outcome_payload=work_models.CanonicalJson(
                    history.encode_transition_receipt_outcome(evidence=None, outcome="continue", candidate=candidate)
                ),
            ),
            replace(
                receipt,
                outcome_payload=work_models.CanonicalJson(
                    history.encode_transition_receipt_outcome(evidence=None, outcome="submit-review")
                ),
            ),
        )
        for malformed in invalid:
            with self.subTest(malformed=malformed), self.assertRaises((ValueError, msgspec.ValidationError)):
                legacy_review_candidate(malformed)

    def test_snapshot_verification_rejects_each_uncorrelated_source(self) -> None:
        snapshot, context, encoded = self.snapshot_context()
        evidence = verify_candidate_snapshot_context(context, None, encoded)
        self.assertEqual(snapshot, evidence.snapshot)

        receipt = context.receipt
        reference = context.reference
        bad_input = msgspec.json.encode(
            CandidateSnapshotReceiptInput(snapshot.candidate, int(reference.artifact_ref_id) + 1),
            order="sorted",
        )
        wrong_outcome = history.encode_transition_receipt_outcome(
            evidence=None,
            outcome="continue",
            candidate=snapshot.candidate,
        )
        failures = (
            (replace(context, receipt=replace(receipt, artifact_ref_id=None)), None, encoded),
            (replace(context, receipt=replace(receipt, input_schema="wrong/v1")), None, encoded),
            (
                replace(context, receipt=replace(receipt, input_payload=work_models.CanonicalJson(bad_input))),
                None,
                encoded,
            ),
            (context, "working-tree-sha256:" + "1" * 64, encoded),
            (replace(context, reference=replace(reference, kind=work_models.ArtifactKind.BRIEF)), None, encoded),
            (context, None, encoded + b"x"),
            (
                replace(
                    context,
                    receipt=replace(receipt, outcome_payload=work_models.CanonicalJson(wrong_outcome)),
                ),
                None,
                encoded,
            ),
            (replace(context, attempt_id=AttemptId("other-attempt")), None, encoded),
            (
                replace(
                    context,
                    state=work_models.AttemptState.REVIEW,
                    candidate_revision=snapshot.candidate,
                    candidate_recorded_at=SQLITE_NOW + timedelta(seconds=1),
                ),
                None,
                encoded,
            ),
        )
        for invalid_context, candidate, value in failures:
            with self.subTest(context=invalid_context), self.assertRaises((ValueError, msgspec.ValidationError)):
                verify_candidate_snapshot_context(invalid_context, candidate, value)

    def test_snapshot_history_rejects_missing_and_orphaned_relationships(self) -> None:
        _snapshot, context, encoded = self.snapshot_context()
        state = complete_sqlite_state()
        correlated = replace(
            state,
            artifact_references=(context.reference,),
            transition_receipts=(context.receipt,),
        )
        self.assertEqual(
            1, len(validate_candidate_snapshot_history(correlated, {context.reference.artifact_ref_id: encoded}))
        )

        missing = (
            replace(correlated, lifecycle=replace(correlated.lifecycle, attempts=())),
            replace(correlated, artifact_references=()),
        )
        for invalid in missing:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "missing its attempt"):
                validate_candidate_snapshot_history(invalid, {})
        with self.assertRaisesRegex(ValueError, "Every review submission"):
            validate_candidate_snapshot_history(
                replace(correlated, transition_receipts=(replace(context.receipt, input_schema="unknown/v1"),)),
                {},
            )
        with self.assertRaisesRegex(ValueError, "not linked from review history"):
            validate_candidate_snapshot_history(
                replace(correlated, transition_receipts=(), lifecycle=replace(correlated.lifecycle, attempts=())),
                {context.reference.artifact_ref_id: encoded},
            )

    def test_working_tree_snapshot_is_canonical_and_restores_with_index(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        observed = read_working_tree_candidate(source)
        snapshot = WorkingTreeCandidateSnapshot(
            "pinboard-candidate-snapshot/v2",
            "attempt-1",
            "item-1",
            observed.identity,
            "main",
            base,
            base,
            "2026-09-15T20:00:00+00:00",
            observed.diff,
        )
        encoded = canonical_candidate_snapshot_bytes(snapshot)
        self.assertEqual(snapshot, decode_candidate_snapshot(encoded))
        self.assertIn("attempt-1-candidate-snapshot-", candidate_snapshot_key(snapshot))

        target = Path(tempfile.mkdtemp()).resolve()
        self.git(target.parent, "clone", "--quiet", str(source), str(target))
        restored = restore_working_tree_candidate(
            target,
            expected_branch="main",
            preimage_revision=base,
            candidate=snapshot.candidate,
            diff=snapshot.diff,
        )
        self.assertEqual(CandidateRestoreSuccess(True, snapshot.candidate), restored)
        self.assertIn("M  tracked.txt", self.git(target, "status", "--short"))
        (target / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(
            "dirty-working-tree",
            self.rejection(
                restore_working_tree_candidate(
                    target,
                    expected_branch="main",
                    preimage_revision=base,
                    candidate=snapshot.candidate,
                    diff=snapshot.diff,
                )
            ).reason,
        )
        (target / "untracked.txt").unlink()
        self.assertEqual(
            CandidateRestoreSuccess(False, snapshot.candidate),
            restore_working_tree_candidate(
                target,
                expected_branch="main",
                preimage_revision=base,
                candidate=snapshot.candidate,
                diff=snapshot.diff,
            ),
        )

    def test_retained_patch_only_snapshot_restores_its_known_preimage_without_relabeling(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("historical candidate\n", encoding="utf-8")
        diff = read_working_tree_candidate(source).diff
        candidate = f"working-tree-sha256:{hashlib.sha256(diff).hexdigest()}"
        snapshot = candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            "attempt-1",
            "item-1",
            candidate,
            "main",
            base,
            base,
            SQLITE_NOW.isoformat(),
            diff,
        )
        encoded = canonical_candidate_snapshot_bytes(snapshot)
        decoded = decode_candidate_snapshot(encoded)
        self.assertEqual(encoded, canonical_candidate_snapshot_bytes(decoded))
        target = self.clone(source)

        restored = candidate_compatibility.restore_working_tree_candidate(
            target,
            expected_branch="main",
            preimage_revision=decoded.preimage_revision,
            candidate=decoded.candidate,
            diff=decoded.diff,
        )

        self.assertEqual(CandidateRestoreSuccess(True, candidate), restored)
        self.assertEqual("historical candidate\n", (target / "tracked.txt").read_text())
        self.assertEqual(base, self.git(target, "rev-parse", "HEAD"))
        self.assertNotEqual(candidate, read_working_tree_candidate(target).identity)

    def test_working_tree_restore_rejects_each_unsafe_precondition_and_reports_changed_failure(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        observed = read_working_tree_candidate(source)
        target = self.clone(source)

        self.assertEqual(
            "wrong-branch",
            self.rejection(
                restore_working_tree_candidate(
                    target,
                    expected_branch="other",
                    preimage_revision=base,
                    candidate=observed.identity,
                    diff=observed.diff,
                )
            ).reason,
        )
        self.assertEqual(
            "wrong-head",
            self.rejection(
                restore_working_tree_candidate(
                    target,
                    expected_branch="main",
                    preimage_revision="0" * 40,
                    candidate=observed.identity,
                    diff=observed.diff,
                )
            ).reason,
        )
        (target / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(
            "dirty-working-tree",
            self.rejection(
                restore_working_tree_candidate(
                    target,
                    expected_branch="main",
                    preimage_revision=base,
                    candidate=observed.identity,
                    diff=observed.diff,
                )
            ).reason,
        )
        (target / "untracked.txt").unlink()
        invalid_diff = b"not a patch\n"
        self.assertEqual(
            "patch-rejected",
            self.rejection(
                restore_working_tree_candidate(
                    target,
                    expected_branch="main",
                    preimage_revision=base,
                    candidate=f"working-tree-sha256:{hashlib.sha256(invalid_diff).hexdigest()}",
                    diff=invalid_diff,
                )
            ).reason,
        )

        with (
            patch("pinboard.adapters.files.root.observe_checkout_identity", return_value=("main", base)),
            patch("pinboard.adapters.files.root._working_tree_status", return_value=b""),
            patch(
                "pinboard.adapters.files.root.read_working_tree_candidate",
                side_effect=(WorkingTreeCandidate("empty", b""), WorkingTreeCandidate("empty", b"")),
            ),
            patch(
                "pinboard.adapters.files.root.subprocess.run",
                return_value=subprocess.CompletedProcess(["git", "apply"], 0, b"", b""),
            ),
            self.assertRaises(CandidateRestoreAfterMutationError),
        ):
            restore_working_tree_candidate(
                target,
                expected_branch="main",
                preimage_revision=base,
                candidate=observed.identity,
                diff=observed.diff,
            )

    def test_commit_snapshot_fast_forwards_only_from_exact_clean_preimage(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self.git(source, "add", "tracked.txt")
        self.git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "candidate")
        candidate = self.git(source, "rev-parse", "HEAD")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate, "--"], cwd=source, check=True, capture_output=True
        ).stdout
        snapshot = CommitCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            "attempt-1",
            "item-1",
            candidate,
            "main",
            base,
            base,
            "2026-09-15T20:00:00+00:00",
            diff,
        )
        self.assertEqual(snapshot, decode_candidate_snapshot(canonical_candidate_snapshot_bytes(snapshot)))

        target = Path(tempfile.mkdtemp()).resolve()
        self.git(source, "worktree", "add", "--detach", str(target), base)
        self.git(target, "switch", "-c", "restore-main")
        rejected = restore_commit_candidate(
            target,
            expected_branch="main",
            preimage_revision=base,
            accepted_base_revision=base,
            candidate=candidate,
            diff=diff,
        )
        self.assertIsInstance(rejected, CandidateRestoreRejection)

    def test_commit_restore_rejects_every_unsafe_candidate_shape(self) -> None:
        source, base = self.repository()
        (source / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self.git(source, "add", "tracked.txt")
        self.git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "candidate")
        candidate = self.git(source, "rev-parse", "HEAD")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, candidate, "--"], cwd=source, check=True, capture_output=True
        ).stdout
        target = Path(tempfile.mkdtemp()).resolve()
        self.git(source, "worktree", "add", "--detach", str(target), base)
        self.git(target, "switch", "-c", "restore-matrix")

        def restore(
            *,
            expected_branch: str = "restore-matrix",
            candidate_revision: str = candidate,
            candidate_diff: bytes = diff,
        ) -> CandidateRestoreSuccess | CandidateRestoreRejection:
            return restore_commit_candidate(
                target,
                expected_branch=expected_branch,
                preimage_revision=base,
                accepted_base_revision=base,
                candidate=candidate_revision,
                diff=candidate_diff,
            )

        self.assertEqual("wrong-branch", self.rejection(restore(expected_branch="other")).reason)
        (target / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual("dirty-working-tree", self.rejection(restore()).reason)
        (target / "untracked.txt").unlink()
        self.git(target, "checkout", "--detach", base)
        (target / "tracked.txt").write_text("divergent\n", encoding="utf-8")
        self.git(target, "add", "tracked.txt")
        self.git(target, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "divergent")
        self.git(target, "switch", "-C", "restore-matrix")
        self.assertEqual("wrong-head", self.rejection(restore()).reason)
        self.git(target, "reset", "--hard", base)
        self.assertEqual("missing-commit", self.rejection(restore(candidate_revision="0" * 40)).reason)
        self.assertEqual("candidate-diff-mismatch", self.rejection(restore(candidate_diff=b"wrong\n")).reason)

        with (
            patch("pinboard.adapters.files.root.observe_checkout_identity", return_value=("restore-matrix", base)),
            patch("pinboard.adapters.files.root._working_tree_status", return_value=b""),
            patch("pinboard.adapters.files.root._git_bytes", return_value=diff),
            patch(
                "pinboard.adapters.files.root.subprocess.run",
                side_effect=(
                    subprocess.CompletedProcess(["git", "cat-file"], 0, b"", b""),
                    subprocess.CompletedProcess(["git", "merge-base"], 0, b"", b""),
                    subprocess.CompletedProcess(["git", "merge"], 1, b"", b"rejected"),
                ),
            ),
        ):
            self.assertEqual("fast-forward-rejected", self.rejection(restore()).reason)

        observed = restore()
        self.assertEqual(CandidateRestoreSuccess(True, candidate), observed)
        self.assertEqual(CandidateRestoreSuccess(False, candidate), restore())
        self.git(target, "reset", "--hard", base)
        with (
            patch(
                "pinboard.adapters.files.root.observe_checkout_identity",
                side_effect=(("restore-matrix", base), ("restore-matrix", base)),
            ),
            self.assertRaises(CandidateRestoreAfterMutationError),
        ):
            restore()

    def test_review_candidate_observation_covers_supported_candidate_shapes(self) -> None:
        store = self.initialized_store()
        checkout = Path(tempfile.mkdtemp()).resolve()
        roots = cli_commands.ResolvedRoots(checkout, checkout, checkout, False)
        attempt_id = AttemptId("work-a-1")
        root_error = RootError(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "unavailable")

        missing = decision_models.SubmitReviewCommand(
            action(decision_models.SubmitReviewAction, AttemptId("missing")),
            work_models.SubmitReviewInput(CandidateId("commit")),
        )
        self.assertIsInstance(
            lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, missing, SQLITE_NOW),
            DecisionFailure,
        )

        working_candidate = working_tree_identity("head", b"diff")
        working = decision_models.SubmitReviewCommand(
            action(decision_models.SubmitReviewAction, attempt_id),
            work_models.SubmitReviewInput(CandidateId(working_candidate)),
        )
        committed = decision_models.SubmitReviewCommand(
            action(decision_models.SubmitReviewAction, attempt_id),
            work_models.SubmitReviewInput(CandidateId("commit")),
        )

        with patch.object(lifecycle_artifacts, "observe_checkout_identity", side_effect=root_error):
            self.assertIsInstance(
                lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, committed, SQLITE_NOW),
                DecisionFailure,
            )
        with patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=("wrong", "head")):
            self.assertIsInstance(
                lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, committed, SQLITE_NOW),
                DecisionFailure,
            )

        observed_identity = ("codex/work-a", "head")
        with (
            patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=observed_identity),
            patch.object(lifecycle_artifacts, "read_working_tree_candidate", side_effect=root_error),
        ):
            self.assertIsInstance(
                lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, working, SQLITE_NOW),
                DecisionFailure,
            )
        with (
            patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=observed_identity),
            patch.object(
                lifecycle_artifacts,
                "read_working_tree_candidate",
                return_value=WorkingTreeCandidate("working-tree-sha256:" + "1" * 64, b"diff"),
            ),
        ):
            self.assertIsInstance(
                lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, working, SQLITE_NOW),
                DecisionFailure,
            )
        with (
            patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=observed_identity),
            patch.object(
                lifecycle_artifacts,
                "read_working_tree_candidate",
                return_value=WorkingTreeCandidate(working_candidate, b"diff"),
            ),
        ):
            snapshot = lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, working, SQLITE_NOW)
        self.assertIsInstance(snapshot, WorkingTreeCandidateSnapshot)

        commit_observations = (
            (CurrentHeadCandidate("commit", b"diff"), CommitCandidateSnapshot),
            (DifferentHeadCandidate("commit", "other"), DecisionFailure),
            (DirtyHeadCandidate("commit"), DecisionFailure),
        )
        for observation, expected_type in commit_observations:
            with (
                self.subTest(observation=observation),
                patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=observed_identity),
                patch.object(lifecycle_artifacts, "read_current_head_candidate", return_value=observation),
            ):
                result = lifecycle_artifacts._observe_review_candidate(
                    roots.source_checkout, store, committed, SQLITE_NOW
                )
                self.assertIsInstance(result, expected_type)
        with (
            patch.object(lifecycle_artifacts, "observe_checkout_identity", return_value=observed_identity),
            patch.object(lifecycle_artifacts, "read_current_head_candidate", side_effect=root_error),
        ):
            self.assertIsInstance(
                lifecycle_artifacts._observe_review_candidate(roots.source_checkout, store, committed, SQLITE_NOW),
                DecisionFailure,
            )

    def test_candidate_restore_reports_every_supported_outcome(self) -> None:
        store = self.initialized_store()
        checkout = Path(tempfile.mkdtemp()).resolve()
        roots = cli_commands.ResolvedRoots(checkout, checkout, checkout, False)
        command = cli_commands.CandidateRestoreCommand(AttemptId("work-a-1"), True)
        snapshot, context, _encoded = self.snapshot_context()
        evidence = CandidateSnapshotEvidence(snapshot, context.reference, context.receipt)

        self.assertIsInstance(candidate_recovery.restore_candidate(roots, store, command), CommandFailure)
        with patch.object(candidate_recovery, "read_reference", return_value=b"invalid"):
            self.assertIsInstance(
                candidate_recovery.read_candidate_evidence_from_context(checkout, context, None), CommandFailure
            )

        rejection = CandidateRestoreRejection("wrong-head", "branch", "head")
        root_error = RootError(RootErrorCode.PROJECT_GIT_CHECKOUT_UNAVAILABLE, "unavailable")
        mutated = CandidateRestoreAfterMutationError(RootErrorCode.PROJECT_GIT_ROOT_UNAVAILABLE, "changed")
        working_outcomes = (
            (rejection, CommandFailure),
            (root_error, CommandFailure),
            (mutated, CommittedEffectFailure),
            (CandidateRestoreSuccess(True, snapshot.candidate), int),
        )
        for outcome, expected_type in working_outcomes:
            with (
                self.subTest(outcome=outcome),
                patch.object(candidate_recovery, "read_candidate_evidence", return_value=evidence),
                patch.object(
                    candidate_recovery,
                    "restore_working_tree_candidate",
                    side_effect=outcome if isinstance(outcome, RootError) else None,
                    return_value=outcome if not isinstance(outcome, RootError) else None,
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = candidate_recovery.restore_candidate(roots, store, command)
                self.assertIsInstance(result, expected_type)
                if isinstance(result, CommittedEffectFailure):
                    self.assertEqual(EffectDisposition.COMMITTED, result.details.effect)
                    self.assertEqual((ChangedSurface.SOURCE_CHECKOUT,), result.details.changed_surfaces)

        commit = CommitCandidateSnapshot(
            "pinboard-candidate-snapshot/v1",
            snapshot.attempt_id,
            snapshot.item_id,
            "commit",
            snapshot.branch,
            snapshot.preimage_revision,
            snapshot.accepted_base_revision,
            snapshot.recorded_at,
            b"diff",
        )
        commit_evidence = CandidateSnapshotEvidence(commit, context.reference, context.receipt)
        with (
            patch.object(candidate_recovery, "read_candidate_evidence", return_value=commit_evidence),
            patch.object(
                candidate_recovery,
                "restore_commit_candidate",
                return_value=CandidateRestoreSuccess(False, commit.candidate),
            ) as restore,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, candidate_recovery.restore_candidate(roots, store, command))
            restore.assert_called_once()

    def test_review_snapshot_publication_preserves_partial_effects(self) -> None:
        store = self.initialized_store()
        checkout = Path(tempfile.mkdtemp()).resolve()
        artifacts = ArtifactRepository(resolve_durable_roots(checkout))
        command = decision_models.SubmitReviewCommand(
            action(decision_models.SubmitReviewAction, AttemptId("work-a-1")),
            work_models.SubmitReviewInput(CandidateId("candidate")),
        )
        snapshot, _context, encoded = self.snapshot_context()
        reference = ArtifactRef(
            work_models.ArtifactKind.EVIDENCE,
            candidate_snapshot_key(snapshot),
            1,
            "artifacts/evidence/candidate/1.json",
            hashlib.sha256(encoded).hexdigest(),
            len(encoded),
        )
        decision_failure = DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "not available", None)
        file_error = FileIOError(FileIOErrorCode.DIRECTORY_SYNC_FAILED, "failed")

        with patch.object(
            lifecycle_artifacts,
            "_observe_review_candidate",
            return_value=decision_failure,
        ):
            self.assertIsInstance(
                lifecycle_artifacts._submit_review(checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW),
                DecisionFailure,
            )

        publication_error = ArtifactAcceptanceAfterPublicationError(
            reference.selector, file_error, (ChangedSurface.IMMUTABLE_ARTIFACT,)
        )
        with (
            patch.object(lifecycle_artifacts, "_observe_review_candidate", return_value=snapshot),
            patch.object(ArtifactRepository, "publish", side_effect=publication_error),
        ):
            self.assertIsInstance(
                lifecycle_artifacts._submit_review(checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW),
                lifecycle_artifacts.PublishedTransitionFailure,
            )
        unexpected = ArtifactAcceptanceAfterPublicationError(reference.selector, RuntimeError("failed"), ())
        with (
            patch.object(lifecycle_artifacts, "_observe_review_candidate", return_value=snapshot),
            patch.object(ArtifactRepository, "publish", side_effect=unexpected),
            self.assertRaises(RuntimeError),
        ):
            lifecycle_artifacts._submit_review(checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW)

        for created in (False, True):
            with (
                self.subTest(created=created),
                patch.object(lifecycle_artifacts, "_observe_review_candidate", return_value=snapshot),
                patch.object(ArtifactRepository, "publish", return_value=ArtifactPublication(reference, created)),
                patch.object(
                    lifecycle_artifacts.service,
                    "decide_and_commit_review_submission",
                    return_value=decision_failure,
                ),
            ):
                result = lifecycle_artifacts._submit_review(
                    checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW
                )
                self.assertIsInstance(result, DecisionFailure)
                self.assertEqual(
                    EffectDisposition.COMMITTED if created else EffectDisposition.UNCHANGED,
                    result.details.effect if result.details is not None else EffectDisposition.UNCHANGED,
                )

        storage_error = StorageError(StorageErrorCode.OPERATION_FAILED, "failed")
        with (
            patch.object(lifecycle_artifacts, "_observe_review_candidate", return_value=snapshot),
            patch.object(ArtifactRepository, "publish", return_value=ArtifactPublication(reference, True)),
            patch.object(
                lifecycle_artifacts.service,
                "decide_and_commit_review_submission",
                side_effect=storage_error,
            ),
        ):
            self.assertIsInstance(
                lifecycle_artifacts._submit_review(checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW),
                lifecycle_artifacts.PublishedTransitionFailure,
            )
        with (
            patch.object(lifecycle_artifacts, "_observe_review_candidate", return_value=snapshot),
            patch.object(ArtifactRepository, "publish", return_value=ArtifactPublication(reference, False)),
            patch.object(
                lifecycle_artifacts.service,
                "decide_and_commit_review_submission",
                side_effect=storage_error,
            ),
            self.assertRaises(StorageError),
        ):
            lifecycle_artifacts._submit_review(checkout, store, artifacts, command, SQLITE_NOW, lambda: SQLITE_NOW)
