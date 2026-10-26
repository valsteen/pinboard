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
from pinboard.adapters.files.errors import ArtifactError, RootError, RootErrorCode
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    checkpoint_packages,
    integration,
    ports,
    query_models,
    work_brief_models,
)
from pinboard.domain import decision_models, history
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
class IntegrationUnavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class IntegrationEvidenceInvalid:
    attempt_id: AttemptId
    reference: str
    reason: str


@dataclass(frozen=True, slots=True)
class IntegrationTargetUnresolved:
    target: str


@dataclass(frozen=True, slots=True)
class IntegrationGitFailure:
    code: RootErrorCode
    diagnostic: str


type IntegrationFailure = (
    IntegrationUnavailable
    | IntegrationEvidenceInvalid
    | IntegrationTargetUnresolved
    | IntegrationGitFailure
    | query_models.DamagedTransitionReceipt
)


@dataclass(frozen=True, slots=True)
class ReviewIntegrationSnapshot:
    snapshot: candidate_snapshots.CandidateSnapshot
    source: integration.ProtectedReview


@dataclass(frozen=True, slots=True)
class CompletionIntegrationSnapshot:
    snapshot: candidate_snapshots.CandidateSnapshot
    source: integration.Completion


@dataclass(frozen=True, slots=True)
class CheckpointIntegrationSnapshot:
    snapshot: candidate_snapshots.CandidateSnapshot
    source: integration.AcceptedCheckpoint
    checkpoint_id: str


type IntegrationSnapshot = ReviewIntegrationSnapshot | CompletionIntegrationSnapshot | CheckpointIntegrationSnapshot


def _read_checkpoint_integration_snapshot(
    work_root: Path, context: integration.CheckpointContext, item_id: str
) -> CheckpointIntegrationSnapshot | IntegrationUnavailable | IntegrationEvidenceInvalid:
    source, receipt, package, candidate = context.source, context.receipt, context.package, context.candidate
    if receipt is None:
        return IntegrationUnavailable("The current attempt has no checkpoint acceptance.")
    if package is None:
        return IntegrationEvidenceInvalid(
            source.attempt_id, "checkpoint package", "The accepted package reference is missing."
        )
    try:
        package_bytes = read_reference(work_root, package)
        selected = checkpoint_packages.validate_selected_checkpoint_review_package(
            receipt, package, package_bytes, attempt_id=str(source.attempt_id), item_id=item_id
        )
    except (ArtifactError, ValueError) as error:
        return IntegrationEvidenceInvalid(source.attempt_id, package.selector, str(error))
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return IntegrationEvidenceInvalid(source.attempt_id, package.selector, selected.message)
    if not isinstance(selected, work_brief_models.CheckpointReviewPackageV3):
        return IntegrationUnavailable("The checkpoint package has no current candidate snapshot reference.")
    identity = selected.candidate_snapshot
    if candidate is None:
        return IntegrationEvidenceInvalid(
            source.attempt_id, identity.selector, "The accepted candidate reference is missing."
        )
    if (
        candidate.kind.value,
        candidate.key,
        candidate.revision,
        candidate.selector,
        candidate.content_sha256,
        candidate.size_bytes,
    ) != (
        identity.kind,
        identity.key,
        identity.revision,
        identity.selector,
        identity.content_sha256,
        identity.size_bytes,
    ):
        return IntegrationEvidenceInvalid(
            source.attempt_id, identity.selector, "The candidate reference differs from the accepted package."
        )
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, candidate))
    except (ArtifactError, msgspec.DecodeError, ValueError) as error:
        return IntegrationEvidenceInvalid(source.attempt_id, candidate.selector, str(error))
    if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
        str(source.attempt_id),
        item_id,
        selected.candidate,
    ):
        return IntegrationEvidenceInvalid(
            source.attempt_id, candidate.selector, "The snapshot differs from the accepted package."
        )
    return CheckpointIntegrationSnapshot(snapshot, source, selected.checkpoint.id)


def _read_integration_snapshot(
    work_root: Path, context: integration.IntegrationContext, item_id: str
) -> IntegrationSnapshot | IntegrationUnavailable | IntegrationEvidenceInvalid | query_models.DamagedTransitionReceipt:
    match context:
        case integration.UnavailableContext(reason=reason):
            return IntegrationUnavailable(reason)
        case integration.InvalidSnapshotContext(attempt_id=attempt_id, candidate=candidate, reason=reason):
            return IntegrationEvidenceInvalid(attempt_id, candidate, reason)
        case integration.DamagedReceiptContext(damage=damage):
            return damage
        case integration.SnapshotContext(source=source, snapshot=snapshot_context):
            if snapshot_context is None:
                return IntegrationUnavailable("The retained review candidate has no accepted snapshot bytes.")
            evidence = read_candidate_evidence_from_context(work_root, snapshot_context, source.candidate)
            if isinstance(evidence, DecisionFailure):
                receipt = snapshot_context.receipt
                try:
                    msgspec.json.decode(
                        bytes(receipt.input_payload),
                        type=candidate_snapshots.CandidateSnapshotReceiptInput,
                        strict=True,
                    )
                    msgspec.json.decode(
                        bytes(receipt.outcome_payload), type=history.TransitionReceiptOutcome, strict=True
                    )
                except msgspec.DecodeError as error:
                    return query_models.DamagedTransitionReceipt(
                        source.attempt_id,
                        receipt.history_id,
                        receipt.committed_at,
                        decision_models.ActionKind.SUBMIT_REVIEW,
                        str(error),
                    )
                return IntegrationEvidenceInvalid(
                    source.attempt_id, snapshot_context.reference.selector, evidence.message
                )
            match source:
                case integration.ProtectedReview():
                    return ReviewIntegrationSnapshot(evidence.snapshot, source)
                case integration.Completion():
                    return CompletionIntegrationSnapshot(evidence.snapshot, source)
                case _ as unreachable:
                    assert_never(unreachable)
        case integration.CheckpointContext():
            return _read_checkpoint_integration_snapshot(work_root, context, item_id)
        case _ as unreachable:
            assert_never(unreachable)


def observe_integration(
    source_checkout: Path, work_root: Path, context: integration.IntegrationContext, item_id: str, target: str
) -> integration.ItemIntegration | IntegrationFailure:
    selected = _read_integration_snapshot(work_root, context, item_id)
    if isinstance(
        selected, (IntegrationUnavailable, IntegrationEvidenceInvalid, query_models.DamagedTransitionReceipt)
    ):
        return selected
    snapshot = selected.snapshot
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
    match selected:
        case ReviewIntegrationSnapshot(source=integration.ProtectedReview(attempt_id=attempt_id)):
            source = integration.ProtectedReviewSource(str(attempt_id), snapshot.candidate, compared_from)
        case CompletionIntegrationSnapshot(source=integration.Completion(attempt_id=attempt_id)):
            source = integration.CompletionSource(str(attempt_id), snapshot.candidate, compared_from)
        case CheckpointIntegrationSnapshot(
            source=integration.AcceptedCheckpoint(attempt_id=attempt_id), checkpoint_id=checkpoint_id
        ):
            source = integration.CheckpointSource(str(attempt_id), snapshot.candidate, compared_from, checkpoint_id)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        observed = root.observe_content_at_target(source_checkout, target, snapshot.diff)
    except RootError as error:
        return IntegrationGitFailure(error.code, str(error))
    if isinstance(observed, root.UnresolvedTarget):
        return IntegrationTargetUnresolved(target)
    return integration.ItemIntegration(
        "pinboard-item-integration/v1",
        str(context.revision),
        item_id,
        target,
        observed.target_revision,
        source,
        observed.presence,
    )


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
