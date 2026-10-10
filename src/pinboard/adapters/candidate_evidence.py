"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from pathlib import Path
from typing import assert_never

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    checkpoint_compatibility_models,
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


def _checkpoint_snapshot_reference(
    work_root: Path,
    store: ports.WorkStore,
    selection: query_models.AcceptedCheckpointCandidateSelection,
) -> (
    stored_state.ArtifactReference
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationCandidateEvidenceInvalid
):
    package_reference = selection.package_reference
    if package_reference is None:
        return query_models.IntegrationCandidateUnavailable(
            query_models.IntegrationUnavailableReason.PACKAGE_WITHOUT_SNAPSHOT
        )
    try:
        package_bytes = read_reference(work_root, package_reference)
    except ArtifactError as error:
        return query_models.IntegrationCandidateEvidenceInvalid(selection.attempt_id, package_reference, str(error))
    package = work_briefs.decode_canonical_checkpoint_review_package(package_bytes)
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id, package_reference, package.message
        )
    if package.attempt_id != str(selection.attempt_id) or package.candidate != selection.candidate_revision:
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            package_reference,
            "The checkpoint package does not match the accepted checkpoint receipt.",
        )
    if isinstance(package, checkpoint_compatibility_models.CheckpointReviewPackage):
        return query_models.IntegrationCandidateUnavailable(
            query_models.IntegrationUnavailableReason.PACKAGE_WITHOUT_SNAPSHOT
        )
    identity = package.candidate_snapshot
    reference = store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision)
    if reference is None:
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            package_reference,
            "The checkpoint package names a candidate snapshot artifact that is not accepted.",
        )
    if (
        reference.selector != identity.selector
        or reference.content_sha256 != identity.content_sha256
        or reference.size_bytes != identity.size_bytes
    ):
        return query_models.IntegrationCandidateEvidenceInvalid(
            selection.attempt_id,
            reference,
            "The checkpoint package's candidate snapshot identity differs from the accepted artifact reference.",
        )
    return reference


def observe_item_integration(
    project_root: Path,
    work_root: Path,
    store: ports.WorkStore,
    selection: query_models.IntegrationCandidateSelection,
    target: str,
) -> query_models.IntegrationObservation:
    """Verify the selected candidate's recorded bytes and observe their content presence in the target tree.

    The source checkout is resolved only once a non-empty recorded diff requires a Git observation;
    an empty diff answers no-change without calling Git.
    """

    if isinstance(selection, query_models.AcceptedCheckpointCandidateSelection):
        selected = _checkpoint_snapshot_reference(work_root, store, selection)
    else:
        selected = store.read_latest_artifact_reference(
            work_models.ArtifactKind.EVIDENCE,
            candidate_snapshots.candidate_snapshot_artifact_key(
                str(selection.attempt_id),
                selection.candidate_revision,
                selection.candidate_recorded_at.isoformat(),
            ),
        )
        if selected is None:
            return query_models.IntegrationCandidateUnavailable(
                query_models.IntegrationUnavailableReason.PRE_SNAPSHOT_CANDIDATE
            )
    if not isinstance(selected, stored_state.ArtifactReference):
        return selected
    reference = selected
    try:
        encoded = read_reference(work_root, reference)
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded)
        if snapshot.attempt_id != str(selection.attempt_id) or snapshot.candidate != selection.candidate_revision:
            raise ValueError("The candidate snapshot does not match the selected candidate.")
    except (ArtifactError, ValueError) as error:
        return query_models.IntegrationCandidateEvidenceInvalid(selection.attempt_id, reference, str(error))
    compared_from = (
        snapshot.accepted_base_revision
        if candidate_snapshots.candidate_kind(snapshot) == "commit"
        else snapshot.preimage_revision
    )
    if not snapshot.diff:
        return query_models.IntegrationObserved(
            selection, compared_from, None, query_models.IntegrationPresence.NO_CHANGE
        )
    try:
        source_checkout = root.resolve_source_checkout_root(project_root)
        observed = root.observe_target_content_presence(source_checkout, target, snapshot.diff)
    except RootError as error:
        return query_models.IntegrationCheckoutUnavailable(error.code.value, str(error))
    match observed:
        case root.UnresolvedTarget():
            return query_models.IntegrationTargetUnresolved(observed.target)
        case root.TargetContentPresent():
            return query_models.IntegrationObserved(
                selection,
                compared_from,
                observed.resolved_revision,
                query_models.IntegrationPresence.CONTENT_PRESENT,
            )
        case root.TargetContentNotPresent():
            return query_models.IntegrationObserved(
                selection,
                compared_from,
                observed.resolved_revision,
                query_models.IntegrationPresence.CONTENT_NOT_PRESENT,
            )
        case _ as unreachable:
            assert_never(unreachable)
