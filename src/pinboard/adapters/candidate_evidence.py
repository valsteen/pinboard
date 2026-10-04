"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    checkpoint_compatibility_models,
    item_integration,
    ports,
    query_models,
    stored_state,
    work_brief_models,
    work_briefs,
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


@dataclass(frozen=True, slots=True)
class IntegrationEvidenceInvalid:
    attempt_id: AttemptId
    reference: stored_state.ArtifactReference
    reason: str


def _integration_checkpoint_reference(
    work_root: Path,
    store: ports.WorkStore,
    selection: item_integration.CheckpointSelection,
) -> stored_state.ArtifactReference | IntegrationEvidenceInvalid | item_integration.CandidateUnavailable:
    reference = selection.reference
    package = work_briefs.decode_canonical_checkpoint_review_package(read_reference(work_root, reference))
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return IntegrationEvidenceInvalid(selection.attempt.attempt_id, reference, package.message)
    if (
        package.attempt_id != str(selection.attempt.attempt_id)
        or package.item_id != str(selection.attempt.item_id)
        or package.candidate != selection.candidate
        or package.checkpoint.id != selection.checkpoint_id
    ):
        return IntegrationEvidenceInvalid(
            selection.attempt.attempt_id, reference, "Checkpoint package differs from its acceptance."
        )
    if isinstance(package, checkpoint_compatibility_models.CheckpointReviewPackage):
        return item_integration.CandidateUnavailable("The checkpoint package has no candidate snapshot reference.")
    identity = package.candidate_snapshot
    snapshot_reference = store.read_artifact_reference(
        work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision
    )
    if snapshot_reference is None:
        return IntegrationEvidenceInvalid(
            selection.attempt.attempt_id, reference, "Accepted checkpoint snapshot reference is missing."
        )
    reference = snapshot_reference
    if (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
    ):
        return IntegrationEvidenceInvalid(
            selection.attempt.attempt_id, reference, "Checkpoint snapshot reference differs from its package."
        )
    return reference


def read_integration_evidence(
    work_root: Path,
    store: ports.WorkStore,
    selection: item_integration.CandidateSelection,
) -> (
    tuple[candidate_snapshots.CandidateSnapshot, item_integration.IntegrationSource]
    | IntegrationEvidenceInvalid
    | item_integration.CandidateUnavailable
):
    """Verify the selected package and snapshot, without loading unrelated review artifacts."""
    reference = selection.reference
    attempt = selection.attempt
    try:
        candidate = attempt.candidate_revision
        if isinstance(selection, item_integration.CheckpointSelection):
            checkpoint_reference = _integration_checkpoint_reference(work_root, store, selection)
            if isinstance(checkpoint_reference, IntegrationEvidenceInvalid | item_integration.CandidateUnavailable):
                return checkpoint_reference
            reference = checkpoint_reference
            candidate = selection.candidate
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
        if (
            snapshot.attempt_id != str(attempt.attempt_id)
            or snapshot.item_id != str(attempt.item_id)
            or snapshot.candidate != candidate
        ):
            return IntegrationEvidenceInvalid(
                attempt.attempt_id, reference, "Snapshot differs from the selected candidate."
            )
        if isinstance(selection, item_integration.ProtectedSelection) and (
            snapshot.branch != attempt.branch
            or snapshot.accepted_base_revision != attempt.base_revision
            or attempt.candidate_recorded_at is None
            or snapshot.recorded_at != attempt.candidate_recorded_at.isoformat()
        ):
            return IntegrationEvidenceInvalid(
                attempt.attempt_id, reference, "Snapshot differs from the protected attempt."
            )
    except (ArtifactError, ValueError) as error:
        return IntegrationEvidenceInvalid(attempt.attempt_id, reference, str(error))
    match snapshot:
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            compared_from = snapshot.accepted_base_revision
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            | candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot()
        ):
            compared_from = snapshot.preimage_revision
        case _ as unreachable:
            assert_never(unreachable)
    source: item_integration.IntegrationSource
    match selection:
        case item_integration.ProtectedSelection():
            source = item_integration.ProtectedReview(snapshot.attempt_id, snapshot.candidate, compared_from)
        case item_integration.CompletionSelection():
            source = item_integration.Completion(snapshot.attempt_id, snapshot.candidate, compared_from)
        case item_integration.CheckpointSelection():
            source = item_integration.AcceptedCheckpoint(
                snapshot.attempt_id, snapshot.candidate, compared_from, selection.checkpoint_id
            )
        case _ as unreachable:
            assert_never(unreachable)
    return snapshot, source


def observe_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    selection: item_integration.CandidateSelection,
    target: str,
) -> (
    item_integration.ItemIntegration
    | IntegrationEvidenceInvalid
    | item_integration.CandidateUnavailable
    | root.IntegrationTargetUnresolved
):
    """Compose verified accepted diff bytes with the read-only Git comparison; RootError names Git failures."""
    evidence = read_integration_evidence(work_root, store, selection)
    if isinstance(evidence, IntegrationEvidenceInvalid | item_integration.CandidateUnavailable):
        return evidence
    snapshot, source = evidence
    observation = root.read_integration_content(source_checkout, target, snapshot.diff)
    if isinstance(observation, root.IntegrationTargetUnresolved):
        return observation
    return item_integration.ItemIntegration(
        "pinboard-item-integration/v1",
        snapshot.item_id,
        target,
        observation.target_revision,
        source,
        observation.presence,
    )
