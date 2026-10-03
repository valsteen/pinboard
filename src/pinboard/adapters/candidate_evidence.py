"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from functools import partial
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
    work_brief_models,
    work_briefs,
)
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


def _verified_checkpoint_snapshot(
    work_root: Path, candidate: query_models.CheckpointCandidateFacts
) -> (
    tuple[candidate_snapshots.CandidateSnapshot, str, str]
    | query_models.IntegrationUnavailableReason
    | query_models.IntegrationEvidenceInvalid
):
    """Return the package-named candidate snapshot, its candidate, and checkpoint id after byte verification."""

    package_reference = candidate.package_reference
    if package_reference is None:
        return query_models.IntegrationEvidenceInvalid(
            candidate.attempt_id,
            f"history {candidate.receipt.history_id} checkpoint package",
            "The checkpoint acceptance names no accepted package reference.",
        )
    try:
        package = work_briefs.decode_canonical_checkpoint_review_package(read_reference(work_root, package_reference))
    except ArtifactError as error:
        return query_models.IntegrationEvidenceInvalid(candidate.attempt_id, package_reference.selector, str(error))
    match package:
        case work_brief_models.WorkBriefFailure(message=message):
            return query_models.IntegrationEvidenceInvalid(candidate.attempt_id, package_reference.selector, message)
        case (
            checkpoint_compatibility_models.CheckpointReviewPackage()
            | checkpoint_compatibility_models.CheckpointReviewPackageV2()
        ):
            return query_models.IntegrationUnavailableReason.PACKAGE_WITHOUT_CANDIDATE_SNAPSHOT
        case work_brief_models.CheckpointReviewPackageV3():
            pass
        case _ as unreachable:
            assert_never(unreachable)
    named = package.candidate_snapshot
    reference = candidate.candidate_reference
    if (
        package.attempt_id != str(candidate.attempt_id)
        or reference is None
        or (reference.key, reference.revision, reference.content_sha256, reference.size_bytes)
        != (named.key, named.revision, named.content_sha256, named.size_bytes)
    ):
        return query_models.IntegrationEvidenceInvalid(
            candidate.attempt_id,
            named.selector,
            "The checkpoint package's candidate snapshot is not the accepted reference for its attempt.",
        )
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
    except (ArtifactError, ValueError) as error:
        return query_models.IntegrationEvidenceInvalid(candidate.attempt_id, reference.selector, str(error))
    if snapshot.attempt_id != str(candidate.attempt_id) or snapshot.candidate != package.candidate:
        return query_models.IntegrationEvidenceInvalid(
            candidate.attempt_id,
            reference.selector,
            "The checkpoint candidate snapshot does not match its package candidate.",
        )
    return snapshot, package.candidate, package.checkpoint.id


def _compared_from_revision(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
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


def _compared_candidate(
    work_root: Path, facts: query_models.ItemIntegrationFacts
) -> (
    tuple[query_models.IntegrationSource, candidate_snapshots.CandidateSnapshot]
    | query_models.IntegrationCandidateUnavailable
    | query_models.IntegrationEvidenceInvalid
):
    """Verify the selected candidate's accepted snapshot bytes and name the source they came from."""

    unavailable = partial(query_models.IntegrationCandidateUnavailable, facts.work_item_id, facts.item_state)
    candidate = facts.candidate
    match candidate:
        case query_models.UnavailableCandidateFacts(reason=reason):
            return unavailable(reason)
        case query_models.DamagedCandidateFacts(attempt_id=attempt_id, defect=defect):
            return query_models.IntegrationEvidenceInvalid(attempt_id, "accepted candidate snapshot", defect)
        case query_models.RetainedCandidateFacts(selection=selection, attempt_id=attempt_id, snapshot=context):
            if context is None:
                return unavailable(query_models.IntegrationUnavailableReason.PRE_SNAPSHOT_CANDIDATE)
            evidence = read_candidate_evidence_from_context(work_root, context, None)
            if isinstance(evidence, DecisionFailure):
                return query_models.IntegrationEvidenceInvalid(attempt_id, context.reference.selector, evidence.message)
            snapshot = evidence.snapshot
            compared_from = _compared_from_revision(snapshot)
            match selection:
                case query_models.IntegrationSourceSelection.PROTECTED_REVIEW:
                    return (
                        query_models.ProtectedReviewSource(str(attempt_id), snapshot.candidate, compared_from),
                        snapshot,
                    )
                case query_models.IntegrationSourceSelection.COMPLETION:
                    return query_models.CompletionSource(str(attempt_id), snapshot.candidate, compared_from), snapshot
                case _ as unreachable:
                    assert_never(unreachable)
        case query_models.CheckpointCandidateFacts(attempt_id=attempt_id):
            verified = _verified_checkpoint_snapshot(work_root, candidate)
            if isinstance(verified, query_models.IntegrationUnavailableReason):
                return unavailable(verified)
            if isinstance(verified, query_models.IntegrationEvidenceInvalid):
                return verified
            snapshot, candidate_revision, checkpoint_id = verified
            return (
                query_models.AcceptedCheckpointSource(
                    str(attempt_id), candidate_revision, _compared_from_revision(snapshot), checkpoint_id
                ),
                snapshot,
            )
        case _ as unreachable:
            assert_never(unreachable)


def observe_item_integration(
    source_checkout: Path,
    work_root: Path,
    facts: query_models.ItemIntegrationFacts,
    target: str,
) -> query_models.ItemIntegrationResult:
    """Compare one selected reviewed candidate's verified diff with a named target's current content.

    An empty recorded diff is no-change without a content check. Git read failures other than an
    unresolved target remain the Git adapter's RootError.
    """

    compared = _compared_candidate(work_root, facts)
    if not isinstance(compared, tuple):
        return compared
    source, snapshot = compared
    if not snapshot.diff:
        resolution = root.resolve_target_revision(source_checkout, target)
        match resolution:
            case root.UnresolvedTarget(diagnostic=diagnostic):
                return query_models.IntegrationTargetUnresolved(target, diagnostic)
            case root.ResolvedTarget(revision=revision):
                return _item_integration(facts, target, revision, source, query_models.IntegrationPresence.NO_CHANGE)
            case _ as unreachable:
                assert_never(unreachable)
    observation = root.observe_target_content(source_checkout, target, snapshot.diff)
    match observation:
        case root.UnresolvedTarget(diagnostic=diagnostic):
            return query_models.IntegrationTargetUnresolved(target, diagnostic)
        case root.TargetContainsDiff(target_revision=revision):
            return _item_integration(facts, target, revision, source, query_models.IntegrationPresence.CONTENT_PRESENT)
        case root.TargetLacksDiff(target_revision=revision):
            return _item_integration(
                facts, target, revision, source, query_models.IntegrationPresence.CONTENT_NOT_PRESENT
            )
        case _ as unreachable:
            assert_never(unreachable)


def _item_integration(
    facts: query_models.ItemIntegrationFacts,
    target: str,
    target_revision: str,
    source: query_models.IntegrationSource,
    presence: query_models.IntegrationPresence,
) -> query_models.ItemIntegration:
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        str(facts.project_revision),
        str(facts.work_item_id),
        target,
        target_revision,
        source,
        presence,
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
