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


@dataclass(frozen=True, slots=True)
class IntegrationEvidenceInvalid:
    attempt_id: AttemptId
    reference: stored_state.ArtifactReference
    defect: str


@dataclass(frozen=True, slots=True)
class CandidateContentObservation:
    source: item_integration.IntegrationSource
    target: root.TargetContentObservation | root.UnresolvedIntegrationTarget


def _read_checkpoint_snapshot_reference(
    work_root: Path,
    store: ports.WorkStore,
    choice: item_integration.AcceptedCheckpointChoice,
) -> stored_state.ArtifactReference | IntegrationEvidenceInvalid | item_integration.CandidateUnavailable:
    reference = choice.reference
    package = work_briefs.decode_canonical_checkpoint_review_package(read_reference(work_root, reference))
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return IntegrationEvidenceInvalid(choice.attempt.attempt_id, reference, package.message)
    if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        return item_integration.CandidateUnavailable("The checkpoint package has no candidate snapshot reference.")
    if (package.attempt_id, package.item_id, package.candidate, package.checkpoint.id) != (
        str(choice.attempt.attempt_id),
        str(choice.attempt.item_id),
        choice.candidate,
        choice.checkpoint_id,
    ):
        return IntegrationEvidenceInvalid(
            choice.attempt.attempt_id,
            reference,
            "The checkpoint package does not match its acceptance receipt.",
        )
    identity = package.candidate_snapshot
    selected_reference = store.read_artifact_reference(
        work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision
    )
    if selected_reference is None:
        return IntegrationEvidenceInvalid(
            choice.attempt.attempt_id, reference, "The checkpoint snapshot reference is missing."
        )
    if (selected_reference.selector, selected_reference.content_sha256, selected_reference.size_bytes) != (
        identity.selector,
        identity.content_sha256,
        identity.size_bytes,
    ):
        return IntegrationEvidenceInvalid(
            choice.attempt.attempt_id,
            selected_reference,
            "The checkpoint snapshot identity differs from its accepted reference.",
        )
    return selected_reference


def read_candidate_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    choice: item_integration.IntegrationChoice,
    target: str,
) -> CandidateContentObservation | IntegrationEvidenceInvalid | item_integration.CandidateUnavailable:
    """Verify one selected snapshot, then observe its diff at the local target."""

    reference = choice.reference
    try:
        match choice:
            case item_integration.AcceptedCheckpointChoice():
                selected_reference = _read_checkpoint_snapshot_reference(work_root, store, choice)
                if isinstance(selected_reference, (IntegrationEvidenceInvalid, item_integration.CandidateUnavailable)):
                    return selected_reference
                reference = selected_reference
                candidate = choice.candidate
            case item_integration.ProtectedReviewChoice() | item_integration.CompletionChoice():
                candidate = choice.attempt.candidate_revision
            case _ as unreachable:
                assert_never(unreachable)
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
        if (
            snapshot.attempt_id,
            snapshot.item_id,
            snapshot.candidate,
        ) != (str(choice.attempt.attempt_id), str(choice.attempt.item_id), candidate):
            return IntegrationEvidenceInvalid(
                choice.attempt.attempt_id,
                reference,
                "The snapshot does not match its selected candidate and accepted reference.",
            )
        if (
            not isinstance(choice, item_integration.AcceptedCheckpointChoice)
            and candidate_snapshots.candidate_snapshot_key(snapshot) != reference.key
        ):
            return IntegrationEvidenceInvalid(
                choice.attempt.attempt_id, reference, "The snapshot key does not match its accepted reference."
            )
        if isinstance(choice, (item_integration.ProtectedReviewChoice, item_integration.CompletionChoice)) and (
            snapshot.branch,
            snapshot.accepted_base_revision,
            snapshot.recorded_at,
        ) != (
            choice.attempt.branch,
            choice.attempt.base_revision,
            None if choice.attempt.candidate_recorded_at is None else choice.attempt.candidate_recorded_at.isoformat(),
        ):
            return IntegrationEvidenceInvalid(
                choice.attempt.attempt_id, reference, "The retained snapshot does not match its selected attempt."
            )
        match snapshot:
            case (
                candidate_snapshots.WorkingTreeCandidateSnapshot()
                | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
            ):
                compared_from = snapshot.preimage_revision
            case candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot():
                compared_from = snapshot.preimage_revision
            case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
                compared_from = snapshot.accepted_base_revision
            case _ as unreachable:
                assert_never(unreachable)
    except (ArtifactError, ValueError, OSError) as error:
        return IntegrationEvidenceInvalid(choice.attempt.attempt_id, reference, str(error))
    source = item_integration.present_integration_source(choice, snapshot.candidate, compared_from)
    return CandidateContentObservation(source, root.read_target_content(source_checkout, target, snapshot.diff))


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
