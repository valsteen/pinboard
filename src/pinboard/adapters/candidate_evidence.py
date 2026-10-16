"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import candidate_snapshot_compatibility_models, candidate_snapshots, ports, query_models
from pinboard.domain.errors import (
    ChangedSurface,
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId


class IntegrationCandidateEvidenceInvalid(ValueError):
    """An accepted candidate reference cannot supply the promised snapshot."""


@dataclass(frozen=True, slots=True)
class IntegrationContentObservation:
    target_revision: str
    presence: query_models.IntegrationPresence
    compared_from: str


def read_candidate_evidence(
    work_root: Path,
    store: ports.WorkStore,
    attempt_id: AttemptId,
    candidate: str | None,
) -> DecisionResult[candidate_snapshots.CandidateSnapshotEvidence]:
    context = store.read_candidate_snapshot_context(attempt_id)
    if context is None:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The attempt has no accepted candidate snapshot.",
            None,
        )
    return read_candidate_evidence_from_context(work_root, context, candidate)


def read_candidate_evidence_from_context(
    work_root: Path,
    context: query_models.CandidateSnapshotContextFacts,
    candidate: str | None,
) -> DecisionResult[candidate_snapshots.CandidateSnapshotEvidence]:
    try:
        encoded = read_reference(work_root, context.reference)
        return candidate_snapshots.verify_candidate_snapshot_context(context, candidate, encoded)
    except (ArtifactError, ValueError) as error:
        return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error), None)


def compare_integration_candidate(
    source_checkout: Path,
    work_root: Path,
    candidate: query_models.IntegrationCandidateFacts,
    target: str,
) -> IntegrationContentObservation | root.IntegrationTargetUnresolved:
    """Combine verified snapshot bytes with the Git adapter's read-only content check."""

    snapshot = _verified_integration_snapshot(work_root, candidate)
    compared_from = _integration_compared_from(snapshot)
    return _observe_integration_content(source_checkout, target, snapshot.diff, compared_from)


def _verified_integration_snapshot(
    work_root: Path, candidate: query_models.IntegrationCandidateFacts
) -> candidate_snapshots.CandidateSnapshot:

    try:
        encoded = read_reference(work_root, candidate.snapshot_reference)
    except ArtifactError as error:
        raise IntegrationCandidateEvidenceInvalid(str(error)) from error
    if (
        len(encoded) != candidate.snapshot_reference.size_bytes
        or hashlib.sha256(encoded).hexdigest() != candidate.snapshot_reference.content_sha256
    ):
        raise IntegrationCandidateEvidenceInvalid("Candidate snapshot bytes differ from the accepted reference.")
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded)
    except (msgspec.DecodeError, ValueError) as error:
        raise IntegrationCandidateEvidenceInvalid(f"Candidate snapshot does not decode: {error}") from error
    if snapshot.attempt_id != str(candidate.attempt_id) or snapshot.candidate != candidate.candidate_revision:
        raise IntegrationCandidateEvidenceInvalid("Candidate snapshot does not match the selected source.")
    return snapshot


def _integration_compared_from(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
    compared_from = snapshot.preimage_revision
    match snapshot:
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            compared_from = snapshot.accepted_base_revision
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            | candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot()
        ):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return compared_from


def _observe_integration_content(
    source_checkout: Path, target: str, diff: bytes, compared_from: str
) -> IntegrationContentObservation | root.IntegrationTargetUnresolved:
    if not diff:
        resolved = root.resolve_integration_target(source_checkout, target)
        match resolved:
            case root.IntegrationTargetUnresolved():
                return resolved
            case str() as target_revision:
                return IntegrationContentObservation(
                    target_revision, query_models.IntegrationPresence.NO_CHANGE, compared_from
                )
            case _ as unreachable:
                assert_never(unreachable)
    observed = root.check_diff_content_at_target(source_checkout, target, diff)
    match observed:
        case root.IntegrationTargetUnresolved():
            return observed
        case root.IntegrationContentPresent():
            return IntegrationContentObservation(
                observed.target_revision, query_models.IntegrationPresence.CONTENT_PRESENT, compared_from
            )
        case root.IntegrationContentNotPresent():
            return IntegrationContentObservation(
                observed.target_revision, query_models.IntegrationPresence.CONTENT_NOT_PRESENT, compared_from
            )
        case _ as unreachable:
            assert_never(unreachable)


def observe_candidate_lineage(
    source_checkout: Path,
    evidence: candidate_snapshots.CandidateSnapshotEvidence,
) -> DecisionResult[query_models.CandidateLineage]:
    snapshot = evidence.snapshot
    excluded = candidate_snapshots.excluded_untracked_paths(snapshot)
    try:
        branch, head = root.observe_candidate_checkout_identity(source_checkout)
        if branch != snapshot.branch:
            return query_models.CandidateLineage.DRIFTED
        match snapshot:
            case (
                candidate_snapshots.WorkingTreeCandidateSnapshot()
                | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            ):
                current = root.read_working_tree_candidate(source_checkout)
                if current.preimage_revision == snapshot.preimage_revision and current.diff == snapshot.diff:
                    return query_models.CandidateLineage.WORKING_TREE_CURRENT
                committed = root.read_current_head_candidate(
                    source_checkout, head, snapshot.preimage_revision, excluded_untracked_paths=excluded
                )
                if isinstance(committed, root.CurrentHeadCandidate) and committed.diff == snapshot.diff:
                    return query_models.CandidateLineage.COMMIT_CURRENT
                return query_models.CandidateLineage.DRIFTED
            case candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot():
                return query_models.CandidateLineage.DRIFTED
            case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
                current = root.read_current_head_candidate(
                    source_checkout,
                    snapshot.candidate,
                    snapshot.accepted_base_revision,
                    excluded_untracked_paths=excluded,
                )
                if isinstance(current, root.CurrentHeadCandidate) and current.diff == snapshot.diff:
                    return query_models.CandidateLineage.COMMIT_CURRENT
                return query_models.CandidateLineage.DRIFTED
            case _ as unreachable:
                assert_never(unreachable)
    except RootError as error:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot reobserve the protected candidate checkout: {error}",
            None,
        )


def restore_candidate(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    attempt_id: AttemptId,
    candidate: str,
) -> DecisionResult[root.CandidateRestoreSuccess]:
    evidence = read_candidate_evidence(work_root, store, attempt_id, candidate)
    if isinstance(evidence, DecisionFailure):
        return evidence
    snapshot = evidence.snapshot
    excluded = candidate_snapshots.excluded_untracked_paths(snapshot)
    try:
        match snapshot:
            case (
                candidate_snapshots.WorkingTreeCandidateSnapshot()
                | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            ):
                restored = root.restore_working_tree_candidate(
                    source_checkout,
                    expected_branch=snapshot.branch,
                    preimage_revision=snapshot.preimage_revision,
                    candidate=snapshot.candidate,
                    diff=snapshot.diff,
                    excluded_untracked_paths=excluded,
                )
            case candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot():
                restored = candidate_compatibility.restore_working_tree_candidate(
                    source_checkout,
                    expected_branch=snapshot.branch,
                    preimage_revision=snapshot.preimage_revision,
                    candidate=snapshot.candidate,
                    diff=snapshot.diff,
                )
            case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
                restored = root.restore_commit_candidate(
                    source_checkout,
                    expected_branch=snapshot.branch,
                    preimage_revision=snapshot.preimage_revision,
                    accepted_base_revision=snapshot.accepted_base_revision,
                    candidate=snapshot.candidate,
                    diff=snapshot.diff,
                    excluded_untracked_paths=excluded,
                )
            case _ as unreachable:
                assert_never(unreachable)
    except root.CandidateRestoreAfterMutationError as error:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            str(error),
            FailureDetails(
                observed=(FailureFact("candidate", snapshot.candidate),),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.COMMITTED,
                changed_surfaces=(ChangedSurface.SOURCE_CHECKOUT,),
                alternatives=(),
            ),
        )
    except RootError as error:
        return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error), None)
    if isinstance(restored, root.CandidateRestoreRejection):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Candidate restore rejected the selected checkout: {restored.reason}.",
            FailureDetails(
                observed=(
                    FailureFact("attempt_id", str(attempt_id)),
                    FailureFact("candidate", snapshot.candidate),
                    FailureFact("branch", restored.branch),
                    FailureFact("head", restored.head),
                ),
                mismatches=(FailureMismatch("checkout", "exact clean recovery preimage", restored.reason),),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return restored
