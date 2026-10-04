"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    ports,
    queries,
    query_models,
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
from pinboard.domain.identifiers import AttemptId, WorkItemId


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
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error), None)


def read_item_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    work_item_id: WorkItemId,
    target: str,
) -> (
    query_models.ItemIntegration
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationReceiptDamaged
    | query_models.IntegrationCandidateEvidenceInvalid
    | root.UnresolvedIntegrationTarget
    | None
):
    """Compose one lifecycle-selected snapshot with the local Git content observation."""

    selection = queries.select_item_integration_source(store, work_item_id)
    if selection is None:
        return None
    if isinstance(selection, (query_models.IntegrationCandidateUnavailable, query_models.IntegrationReceiptDamaged)):
        return selection

    evidence = _integration_candidate_evidence(work_root, store, work_item_id, selection)
    if isinstance(
        evidence,
        (
            query_models.IntegrationCandidateUnavailable,
            query_models.IntegrationCandidateEvidenceInvalid,
        ),
    ):
        return evidence
    snapshot = evidence.snapshot if isinstance(evidence, candidate_snapshots.CandidateSnapshotEvidence) else evidence
    source = _integration_source(selection, snapshot)
    observation = root.observe_content_integration(source_checkout, target, snapshot.diff)
    if isinstance(observation, root.UnresolvedIntegrationTarget):
        return observation
    if not snapshot.diff:
        presence = query_models.IntegrationPresence.NO_CHANGE
    elif observation.presence == root.ContentIntegrationPresence.PRESENT:
        presence = query_models.IntegrationPresence.CONTENT_PRESENT
    else:
        presence = query_models.IntegrationPresence.CONTENT_NOT_PRESENT
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        str(work_item_id),
        observation.target,
        observation.revision,
        source,
        presence,
    )


def _integration_candidate_evidence(
    work_root: Path,
    store: ports.WorkStore,
    work_item_id: WorkItemId,
    selection: (
        query_models.ProtectedReviewIntegrationSelection
        | query_models.AcceptedCheckpointIntegrationSelection
        | query_models.CompletionIntegrationSelection
    ),
) -> (
    candidate_snapshots.CandidateSnapshotEvidence
    | candidate_snapshots.CandidateSnapshot
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationCandidateEvidenceInvalid
):
    match selection:
        case query_models.ProtectedReviewIntegrationSelection() | query_models.CompletionIntegrationSelection():
            attempt_id = selection.attempt_id
            candidate = selection.candidate_revision
            context = store.read_candidate_snapshot_context(attempt_id)
            accepted_reference = "current candidate snapshot"
        case query_models.AcceptedCheckpointIntegrationSelection():
            return _checkpoint_candidate_context(work_root, store, work_item_id, selection)
        case _ as unreachable:
            assert_never(unreachable)
    if context is None:
        return query_models.IntegrationCandidateUnavailable(
            work_item_id, selection.item_state, f"{accepted_reference} has no accepted candidate snapshot bytes"
        )
    verified = read_candidate_evidence_from_context(work_root, context, candidate)
    if isinstance(verified, DecisionFailure):
        return query_models.IntegrationCandidateEvidenceInvalid(
            attempt_id, context.reference.artifact_ref_id, context.reference.selector, verified.message
        )
    return verified


def _checkpoint_candidate_context(
    work_root: Path,
    store: ports.WorkStore,
    work_item_id: WorkItemId,
    selection: query_models.AcceptedCheckpointIntegrationSelection,
) -> (
    candidate_snapshots.CandidateSnapshot
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationCandidateEvidenceInvalid
):
    package_reference = selection.package_reference
    reference_id = package_reference.artifact_ref_id
    try:
        encoded = read_reference(work_root, package_reference)
        package = work_briefs.decode_canonical_checkpoint_review_package(encoded)
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id, reference_id, package_reference.selector, str(error)
        )
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id, reference_id, package_reference.selector, package.message
        )
    if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        return query_models.IntegrationCandidateUnavailable(
            work_item_id, selection.item_state, "the accepted checkpoint package has no candidate snapshot reference"
        )
    if (
        package.attempt_id != str(selection.attempt_id)
        or package.item_id != str(work_item_id)
        or package.checkpoint.id != selection.checkpoint_id
        or package.candidate != selection.candidate_revision
    ):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            reference_id,
            package_reference.selector,
            "the accepted checkpoint package does not match its receipt",
        )
    identity = package.candidate_snapshot
    reference = store.read_artifact_reference(work_models.ArtifactKind(identity.kind), identity.key, identity.revision)
    if reference is None or (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
    ):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            reference_id,
            identity.selector,
            "the checkpoint package candidate reference is not accepted or does not match",
        )
    try:
        encoded = read_reference(work_root, reference)
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded)
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id, reference_id, identity.selector, str(error)
        )
    if (
        snapshot.attempt_id != str(selection.attempt_id)
        or snapshot.item_id != str(work_item_id)
        or snapshot.candidate != selection.candidate_revision
    ):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            reference_id,
            identity.selector,
            "the accepted checkpoint candidate snapshot does not match its package",
        )
    return snapshot


def _integration_source(
    selection: (
        query_models.ProtectedReviewIntegrationSelection
        | query_models.AcceptedCheckpointIntegrationSelection
        | query_models.CompletionIntegrationSelection
    ),
    snapshot: candidate_snapshots.CandidateSnapshot,
) -> query_models.IntegrationCandidateSource:
    compared_from = (
        snapshot.preimage_revision
        if candidate_snapshots.candidate_kind(snapshot) == "working-tree"
        else snapshot.accepted_base_revision
    )
    match selection:
        case query_models.ProtectedReviewIntegrationSelection():
            return query_models.ProtectedReviewCandidate(str(selection.attempt_id), snapshot.candidate, compared_from)
        case query_models.AcceptedCheckpointIntegrationSelection():
            return query_models.AcceptedCheckpointCandidate(
                str(selection.attempt_id), selection.checkpoint_id, snapshot.candidate, compared_from
            )
        case query_models.CompletionIntegrationSelection():
            return query_models.CompletionCandidate(str(selection.attempt_id), snapshot.candidate, compared_from)
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
