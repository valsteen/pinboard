"""Verify accepted candidate bytes, observe their content at an integration target, and restore a selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
Integration observation only reads: it composes verified snapshot bytes with the Git adapter's one target-content read.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError, RootErrorCode
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    checkpoint_packages,
    ports,
    queries,
    query_models,
    stored_state,
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
class IntegrationGitFailure:
    """The integration read failed in Git for a reason other than an unresolved target."""

    code: RootErrorCode
    diagnostic: str


@dataclass(frozen=True, slots=True)
class _IntegrationSubject:
    project_revision: int
    item_id: WorkItemId
    source: query_models.IntegrationSource
    diff: bytes


def _evidence_invalid(attempt_id: str, reference: stored_state.ArtifactReference, defect: str) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.INTEGRATION_CANDIDATE_EVIDENCE_INVALID,
        f"Accepted candidate evidence for attempt '{attempt_id}' failed verification: {defect}",
        FailureDetails(
            observed=(
                FailureFact("attempt_id", attempt_id),
                FailureFact("artifact_ref_id", int(reference.artifact_ref_id)),
                FailureFact("selector", reference.selector),
            ),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _verified_context_snapshot(
    work_root: Path,
    context: query_models.CandidateSnapshotContextFacts,
) -> DecisionResult[candidate_snapshots.CandidateSnapshot]:
    evidence = read_candidate_evidence_from_context(work_root, context, None)
    if isinstance(evidence, DecisionFailure):
        return _evidence_invalid(str(context.attempt_id), context.reference, evidence.message)
    return evidence.snapshot


def _verified_checkpoint_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    candidate: query_models.AcceptedCheckpointCandidate,
) -> DecisionResult[tuple[candidate_snapshots.CandidateSnapshot, str]]:
    """Verify the latest checkpoint acceptance's package, then only its named candidate snapshot."""

    unavailable = queries.integration_candidate_unavailable(
        candidate.work_item_id,
        candidate.item_state,
        query_models.IntegrationUnavailableReason.CHECKPOINT_WITHOUT_SNAPSHOT,
    )
    package_reference = candidate.checkpoint.package_reference
    if package_reference is None:
        return unavailable
    attempt_id = str(candidate.attempt_id)
    try:
        package = checkpoint_packages.validate_selected_checkpoint_review_package(
            candidate.checkpoint.receipt,
            package_reference,
            read_reference(work_root, package_reference),
            attempt_id=attempt_id,
            item_id=str(candidate.work_item_id),
        )
    except ArtifactError as error:
        return _evidence_invalid(attempt_id, package_reference, str(error))
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return _evidence_invalid(attempt_id, package_reference, package.message)
    if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        return unavailable
    identity = package.candidate_snapshot
    snapshot_reference = store.read_artifact_reference(
        work_models.ArtifactKind(identity.kind), identity.key, identity.revision
    )
    if snapshot_reference is None:
        return _evidence_invalid(
            attempt_id, package_reference, "The package candidate snapshot has no accepted reference."
        )
    if (
        identity.key != f"{attempt_id}-{package.checkpoint.id}-candidate"
        or identity.revision != 1
        or (snapshot_reference.selector, snapshot_reference.content_sha256, snapshot_reference.size_bytes)
        != (identity.selector, identity.content_sha256, identity.size_bytes)
    ):
        return _evidence_invalid(
            attempt_id,
            snapshot_reference,
            "The package candidate snapshot identity does not match its accepted reference.",
        )
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, snapshot_reference))
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        return _evidence_invalid(attempt_id, snapshot_reference, str(error))
    if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
        package.attempt_id,
        package.item_id,
        package.candidate,
    ):
        return _evidence_invalid(attempt_id, snapshot_reference, "The candidate snapshot does not match its package.")
    return snapshot, package.checkpoint.id


def _integration_subject(
    work_root: Path,
    store: ports.WorkStore,
    candidate: query_models.IntegrationCandidate,
) -> DecisionResult[_IntegrationSubject]:
    match candidate:
        case query_models.ProtectedReviewCandidate(project_revision=revision, snapshot=context):
            verified = _verified_context_snapshot(work_root, context)
            if isinstance(verified, DecisionFailure):
                return verified
            source: query_models.IntegrationSource = query_models.ProtectedReviewSource(
                verified.attempt_id, verified.candidate, candidate_snapshots.compared_from_revision(verified)
            )
            return _IntegrationSubject(revision, context.work_item_id, source, verified.diff)
        case query_models.CompletionCandidate(project_revision=revision, snapshot=context):
            verified = _verified_context_snapshot(work_root, context)
            if isinstance(verified, DecisionFailure):
                return verified
            source = query_models.CompletionSource(
                verified.attempt_id, verified.candidate, candidate_snapshots.compared_from_revision(verified)
            )
            return _IntegrationSubject(revision, context.work_item_id, source, verified.diff)
        case query_models.AcceptedCheckpointCandidate(project_revision=revision, work_item_id=item_id):
            checkpoint = _verified_checkpoint_snapshot(work_root, store, candidate)
            if isinstance(checkpoint, DecisionFailure):
                return checkpoint
            verified, checkpoint_id = checkpoint
            source = query_models.AcceptedCheckpointSource(
                verified.attempt_id,
                verified.candidate,
                candidate_snapshots.compared_from_revision(verified),
                checkpoint_id,
            )
            return _IntegrationSubject(revision, item_id, source, verified.diff)
        case _ as unreachable:
            assert_never(unreachable)


def observe_integration(
    project_root: Path,
    work_root: Path,
    store: ports.WorkStore,
    candidate: query_models.IntegrationCandidate,
    target: str,
) -> DecisionResult[query_models.ItemIntegration] | IntegrationGitFailure:
    """Verify the selected candidate's accepted snapshot, then read its content at the named target."""

    subject = _integration_subject(work_root, store, candidate)
    if isinstance(subject, DecisionFailure):
        return subject
    try:
        observation = root.observe_target_content(project_root, target, subject.diff)
    except RootError as error:
        return IntegrationGitFailure(error.code, error.detail)
    match observation:
        case root.TargetUnresolved():
            return DecisionFailure(
                DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
                f"Integration target '{target}' does not resolve to a commit in the checkout at {project_root}.",
                FailureDetails(
                    observed=(FailureFact("target", target), FailureFact("project_root", str(project_root))),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        case root.TargetContentPresent(target_revision=revision):
            presence = (
                query_models.IntegrationPresence.CONTENT_PRESENT
                if subject.diff
                else query_models.IntegrationPresence.NO_CHANGE
            )
        case root.TargetContentNotPresent(target_revision=revision):
            presence = query_models.IntegrationPresence.CONTENT_NOT_PRESENT
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        str(subject.project_revision),
        str(subject.item_id),
        target,
        revision,
        subject.source,
        presence,
    )
