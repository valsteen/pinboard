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
from pinboard.application import candidate_snapshot_compatibility_models, candidate_snapshots, ports, query_models
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


def _attempt_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    item: query_models.ItemStatusItemFacts,
    attempt_id: AttemptId,
    candidate_revision: str,
) -> (
    candidate_snapshots.CandidateSnapshot
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationEvidenceInvalid
):
    """Verify the snapshot retained for an attempt's protected or closing candidate."""

    try:
        context = store.read_candidate_snapshot_context(attempt_id)
    except ports.WorkStoreError as error:
        return query_models.IntegrationEvidenceInvalid(
            attempt_id, f"candidate snapshot for {candidate_revision}", str(error)
        )
    if context is None:
        return query_models.IntegrationCandidateUnavailable(
            item.work_item_id,
            item.state,
            "The reviewed candidate predates accepted snapshot evidence, so its content cannot be compared.",
        )
    evidence = read_candidate_evidence_from_context(work_root, context, candidate_revision)
    if isinstance(evidence, DecisionFailure):
        return query_models.IntegrationEvidenceInvalid(
            attempt_id, f"candidate snapshot for {candidate_revision}", evidence.message
        )
    return evidence.snapshot


def _checkpoint_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    item: query_models.ItemStatusItemFacts,
    selection: query_models.AcceptedCheckpointSource,
) -> (
    candidate_snapshots.CandidateSnapshot
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationEvidenceInvalid
):
    """Verify the accepted candidate snapshot that a checkpoint package names by its canonical key."""

    key = f"{selection.attempt_id}-{selection.checkpoint_id}-candidate"
    reference = store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, key, 1)
    if reference is None:
        return query_models.IntegrationCandidateUnavailable(
            item.work_item_id,
            item.state,
            f"Checkpoint '{selection.checkpoint_id}' has no accepted candidate snapshot reference.",
        )
    try:
        encoded = read_reference(work_root, reference)
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded)
    except (ArtifactError, msgspec.DecodeError, ValueError) as error:
        return query_models.IntegrationEvidenceInvalid(selection.attempt_id, key, str(error))
    if snapshot.attempt_id != str(selection.attempt_id):
        return query_models.IntegrationEvidenceInvalid(
            selection.attempt_id, key, "The checkpoint candidate snapshot names another attempt."
        )
    return snapshot


def observe_integration_target(
    work_root: Path,
    store: ports.WorkStore,
    source_checkout: Path,
    item: query_models.ItemStatusItemFacts,
    selection: query_models.IntegrationSourceSelection,
    target: str,
) -> query_models.IntegrationOutcome:
    """Compare one selected candidate's verified snapshot diff with a named target's current commit."""

    match selection:
        case query_models.ProtectedReviewSource() | query_models.CompletionSource():
            snapshot = _attempt_snapshot(work_root, store, item, selection.attempt_id, selection.candidate_revision)
        case query_models.AcceptedCheckpointSource():
            snapshot = _checkpoint_snapshot(work_root, store, item, selection)
        case _ as unreachable:
            assert_never(unreachable)
    if isinstance(snapshot, query_models.IntegrationCandidateUnavailable | query_models.IntegrationEvidenceInvalid):
        return snapshot
    try:
        observed = root.observe_target_presence(source_checkout, target, snapshot.diff)
    except RootError as error:
        return query_models.IntegrationGitFailure(error.code.value, str(error))
    presence: query_models.IntegrationPresence
    match observed:
        case root.UnresolvedTargetObservation():
            return query_models.IntegrationTargetUnresolved(target)
        case root.ResolvedTargetWithoutChange(revision=revision):
            presence = query_models.IntegrationPresence.NO_CHANGE
        case root.ResolvedTargetPresence(revision=revision, present=present):
            presence = (
                query_models.IntegrationPresence.CONTENT_PRESENT
                if present
                else query_models.IntegrationPresence.CONTENT_NOT_PRESENT
            )
        case _ as unreachable:
            assert_never(unreachable)
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
    return query_models.IntegrationObservation(selection, snapshot.candidate, compared_from, revision, presence)


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
