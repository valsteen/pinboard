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
    integration,
    ports,
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


def read_integration_candidate(  # noqa: C901, PLR0912 - source-specific package and snapshot verification
    work_root: Path,
    store: ports.WorkStore,
    facts: integration.Facts,
    selected: integration.Selection,
) -> (
    tuple[candidate_snapshots.CandidateSnapshot, integration.Source]
    | integration.InvalidEvidence
    | integration.Unavailable
):
    """Verify only the selected package and candidate bytes, without walking review history."""

    match selected:
        case integration.ProtectedReview() | integration.Completion():
            reference = selected.reference
        case integration.AcceptedCheckpoint():
            reference = selected.package_reference
            try:
                package = work_briefs.decode_canonical_checkpoint_review_package(read_reference(work_root, reference))
                if isinstance(package, work_brief_models.WorkBriefFailure):
                    return integration.InvalidEvidence(selected.attempt_id, reference, package.message)
                if (package.attempt_id, package.item_id, package.candidate, package.checkpoint.id) != (
                    str(selected.attempt_id),
                    str(facts.item.item_id),
                    selected.candidate,
                    selected.checkpoint_id,
                ):
                    return integration.InvalidEvidence(
                        selected.attempt_id, reference, "The checkpoint package does not match its acceptance receipt."
                    )
                if isinstance(package, checkpoint_compatibility_models.CheckpointReviewPackage):
                    return integration.Unavailable(
                        facts.item.item_id,
                        facts.item.state,
                        "The checkpoint package has no candidate snapshot reference.",
                    )
                identity = package.candidate_snapshot
                snapshot_reference = store.read_artifact_reference(
                    work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision
                )
                if snapshot_reference is None or (
                    snapshot_reference.selector,
                    snapshot_reference.content_sha256,
                    snapshot_reference.size_bytes,
                ) != (identity.selector, identity.content_sha256, identity.size_bytes):
                    return integration.InvalidEvidence(
                        selected.attempt_id,
                        reference,
                        "The checkpoint candidate reference does not match accepted evidence.",
                    )
                reference = snapshot_reference
            except (ArtifactError, ValueError) as error:
                return integration.InvalidEvidence(selected.attempt_id, reference, str(error))
        case _ as unreachable:
            assert_never(unreachable)
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
        if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
            str(selected.attempt_id),
            str(facts.item.item_id),
            selected.candidate,
        ):
            return integration.InvalidEvidence(
                selected.attempt_id,
                reference,
                "The accepted snapshot does not match the selected attempt, item and candidate.",
            )
    except (ArtifactError, ValueError) as error:
        return integration.InvalidEvidence(selected.attempt_id, reference, str(error))
    match snapshot:
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
        ):
            compared_from = snapshot.preimage_revision
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            compared_from = snapshot.accepted_base_revision
        case candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot():
            return integration.Unavailable(
                facts.item.item_id,
                facts.item.state,
                "The retained candidate lacks a full working-tree snapshot identity.",
            )
        case _ as unreachable:
            assert_never(unreachable)
    return snapshot, integration.present_source(selected, compared_from)


def observe_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    facts: integration.Facts,
    selected: integration.Selection,
    target: str,
) -> (
    integration.ItemIntegration
    | integration.InvalidEvidence
    | integration.Unavailable
    | root.IntegrationTargetUnresolved
):
    """Compose accepted candidate bytes with the read-only Git observation; RootError remains the Git effect boundary."""

    verified = read_integration_candidate(work_root, store, facts, selected)
    if isinstance(verified, (integration.InvalidEvidence, integration.Unavailable)):
        return verified
    snapshot, source = verified
    observed = root.read_integration_content(source_checkout, target, snapshot.diff)
    if isinstance(observed, root.IntegrationTargetUnresolved):
        return observed
    return integration.ItemIntegration(
        "pinboard-item-integration/v1",
        str(facts.item.item_id),
        target,
        observed.target_revision,
        source,
        observed.presence,
    )
