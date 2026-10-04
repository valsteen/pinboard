"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    checkpoint_compatibility_models,
    checkpoint_packages,
    ports,
    query_models,
    work_brief_models,
)
from pinboard.domain import work_models
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


@dataclass(frozen=True, slots=True)
class CandidateIntegrationRead:
    candidate_revision: str
    compared_from_revision: str
    target_observation: root.IntegrationTargetObservation


@dataclass(frozen=True, slots=True)
class CandidateIntegrationUnavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateIntegrationInvalid:
    accepted_reference: str
    defect: str


type CandidateIntegrationResult = (
    CandidateIntegrationRead | CandidateIntegrationUnavailable | CandidateIntegrationInvalid
)


def read_item_candidate_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    selected: query_models.IntegrationCandidate,
    target: str,
) -> CandidateIntegrationResult:
    """Verify the selected immutable snapshot, then compare its diff with one local target tree."""

    match selected:
        case query_models.ProtectedReviewIntegrationCandidate(snapshot=context):
            candidate = context.candidate_revision
            context_result = context
            checkpoint_snapshot = None
        case query_models.CompletionIntegrationCandidate(candidate_revision=candidate, snapshot=context):
            context_result = context
            checkpoint_snapshot = None
        case query_models.AcceptedCheckpointIntegrationCandidate() as checkpoint:
            checkpoint_evidence = _read_checkpoint_candidate_snapshot(work_root, store, checkpoint)
            if isinstance(checkpoint_evidence, CandidateIntegrationUnavailable | CandidateIntegrationInvalid):
                return checkpoint_evidence
            checkpoint_snapshot, candidate = checkpoint_evidence
            context_result = None
        case _ as unreachable:
            assert_never(unreachable)

    if candidate is None:
        assert context_result is not None
        return CandidateIntegrationInvalid(
            context_result.reference.selector,
            "The selected lifecycle source has no candidate revision.",
        )
    if checkpoint_snapshot is not None:
        snapshot = checkpoint_snapshot
    else:
        assert context_result is not None
        try:
            encoded = read_reference(work_root, context_result.reference)
            evidence = candidate_snapshots.verify_candidate_snapshot_context(context_result, candidate, encoded)
        except (ArtifactError, ValueError, msgspec.DecodeError) as error:
            return CandidateIntegrationInvalid(context_result.reference.selector, str(error))
        snapshot = evidence.snapshot
    compared_from_revision = _candidate_compared_from(snapshot)
    observation = root.read_candidate_integration(source_checkout, target, snapshot.diff)
    return CandidateIntegrationRead(snapshot.candidate, compared_from_revision, observation)


def _read_checkpoint_candidate_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    checkpoint: query_models.AcceptedCheckpointIntegrationCandidate,
) -> tuple[candidate_snapshots.CandidateSnapshot, str] | CandidateIntegrationUnavailable | CandidateIntegrationInvalid:
    try:
        package_bytes = read_reference(work_root, checkpoint.package_reference)
    except (ArtifactError, ValueError) as error:
        return CandidateIntegrationInvalid(
            checkpoint.package_reference.selector,
            f"The accepted checkpoint package bytes cannot be verified: {error}",
        )
    package = checkpoint_packages.validate_selected_checkpoint_review_package(
        checkpoint.receipt,
        checkpoint.package_reference,
        package_bytes,
        attempt_id=str(checkpoint.attempt_id),
        item_id=str(checkpoint.work_item_id),
    )
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return CandidateIntegrationInvalid(checkpoint.package_reference.selector, package.message)
    match package:
        case (
            work_brief_models.CheckpointReviewPackageV3() | checkpoint_compatibility_models.CheckpointReviewPackageV2()
        ):
            snapshot_identity = package.candidate_snapshot
            candidate = package.candidate
        case checkpoint_compatibility_models.CheckpointReviewPackage():
            return CandidateIntegrationUnavailable(
                "the accepted checkpoint package predates candidate snapshot references"
            )
        case _ as unreachable:
            assert_never(unreachable)
    reference = store.read_artifact_reference(
        work_models.ArtifactKind.EVIDENCE,
        snapshot_identity.key,
        snapshot_identity.revision,
    )
    if reference is None:
        return CandidateIntegrationInvalid(
            snapshot_identity.selector,
            "The package candidate snapshot reference is missing from the artifact ledger.",
        )
    package_identity = (
        snapshot_identity.role,
        snapshot_identity.kind,
        snapshot_identity.key,
        snapshot_identity.revision,
        snapshot_identity.selector,
        snapshot_identity.content_sha256,
        snapshot_identity.size_bytes,
    )
    stored_identity = (
        "candidate",
        reference.kind.value,
        reference.key,
        reference.revision,
        reference.selector,
        reference.content_sha256,
        reference.size_bytes,
    )
    if package_identity != stored_identity:
        return CandidateIntegrationInvalid(
            snapshot_identity.selector,
            "The package candidate snapshot identity differs from its accepted artifact reference.",
        )
    try:
        encoded_snapshot = read_reference(work_root, reference)
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded_snapshot)
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        return CandidateIntegrationInvalid(snapshot_identity.selector, str(error))
    if (
        snapshot.attempt_id != str(checkpoint.attempt_id)
        or snapshot.item_id != str(checkpoint.work_item_id)
        or snapshot.candidate != candidate
    ):
        return CandidateIntegrationInvalid(
            snapshot_identity.selector,
            "The accepted checkpoint snapshot does not match its attempt, item, and candidate.",
        )
    return snapshot, candidate


def _candidate_compared_from(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
    match snapshot:
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            | candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot()
        ):
            return snapshot.preimage_revision
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            return snapshot.accepted_base_revision
        case _ as unreachable:
            assert_never(unreachable)


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
