"""Verify accepted candidate bytes for content observation and exact restoration.

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
    item_integration,
    ports,
    query_models,
    stored_state,
    work_brief_models,
)
from pinboard.domain import decision_models, history, work_models
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
class IntegrationFailure:
    code: item_integration.IntegrationFailureCode | RootErrorCode
    message: str
    details: FailureDetails
    recovery: str


@dataclass(frozen=True, slots=True)
class VerifiedIntegrationSource:
    snapshot: candidate_snapshots.CandidateSnapshot
    source: item_integration.Source


def _candidate_unavailable(facts: item_integration.Facts, reason: str) -> IntegrationFailure:
    return IntegrationFailure(
        item_integration.IntegrationFailureCode.CANDIDATE_UNAVAILABLE,
        reason,
        FailureDetails(
            observed=(
                FailureFact("item_id", str(facts.item_id)),
                FailureFact("state", facts.state.value),
                FailureFact("reason", reason),
            ),
            mismatches=(),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
        "Read pinboard_item_status with operation item and this item_id.",
    )


def _integration_evidence_invalid(
    attempt_id: AttemptId, reference: stored_state.ArtifactReference | None, defect: str
) -> IntegrationFailure:
    return IntegrationFailure(
        item_integration.IntegrationFailureCode.CANDIDATE_EVIDENCE_INVALID,
        defect,
        FailureDetails(
            observed=(
                FailureFact("attempt_id", str(attempt_id)),
                FailureFact("accepted_reference", None if reference is None else reference.selector),
            ),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
        "Run pinboard validate to diagnose the accepted evidence; do not replay a mutation.",
    )


def _compared_from_revision(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
    match snapshot:
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
        ):
            return snapshot.preimage_revision
        case (
            candidate_snapshots.CommitCandidateSnapshot()
            | candidate_snapshots.DeclaredCommitCandidateSnapshot()
            | candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot()
        ):
            return snapshot.accepted_base_revision
        case _ as unreachable:
            assert_never(unreachable)


def _read_checkpoint_integration_source(
    work_root: Path,
    store: ports.WorkStore,
    facts: item_integration.Facts,
    selection: item_integration.CheckpointSelection,
) -> VerifiedIntegrationSource | IntegrationFailure | query_models.DamagedTransitionReceipt:
    receipt = store.read_latest_checkpoint_receipt(selection.attempt_id)
    if receipt is None:
        return _candidate_unavailable(
            facts, "The current attempt has no protected candidate and no checkpoint acceptance."
        )
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
        if msgspec.json.encode(outcome, order="sorted") != bytes(receipt.outcome_payload):
            raise ValueError("Checkpoint outcome is not canonical.")
    except (msgspec.DecodeError, ValueError) as error:
        return query_models.DamagedTransitionReceipt(
            selection.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            str(error),
        )
    reference = (
        None if receipt.artifact_ref_id is None else store.read_artifact_reference_by_id(receipt.artifact_ref_id)
    )
    try:
        if reference is None:
            raise ValueError("The checkpoint acceptance has no accepted package reference.")
        package = checkpoint_packages.validate_selected_checkpoint_review_package(
            receipt,
            reference,
            read_reference(work_root, reference),
            attempt_id=str(selection.attempt_id),
            item_id=str(facts.item_id),
        )
        if isinstance(package, work_brief_models.WorkBriefFailure):
            raise ValueError(package.message)
        if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
            return _candidate_unavailable(
                facts, "The checkpoint package has no supported candidate snapshot reference."
            )
        identity = package.candidate_snapshot
        reference = store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision)
        if reference is None or (reference.selector, reference.content_sha256, reference.size_bytes) != (
            identity.selector,
            identity.content_sha256,
            identity.size_bytes,
        ):
            raise ValueError("The checkpoint candidate reference differs from its accepted package identity.")
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
        if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
            package.attempt_id,
            package.item_id,
            package.candidate,
        ):
            raise ValueError("The checkpoint candidate snapshot does not match its package.")
        source = item_integration.AcceptedCheckpoint(
            str(selection.attempt_id), snapshot.candidate, _compared_from_revision(snapshot), package.checkpoint.id
        )
        return VerifiedIntegrationSource(snapshot, source)
    except (ArtifactError, ValueError) as error:
        return _integration_evidence_invalid(selection.attempt_id, reference, str(error))


def _read_integration_source(
    work_root: Path, store: ports.WorkStore, facts: item_integration.Facts, selection: item_integration.Selection
) -> VerifiedIntegrationSource | IntegrationFailure | query_models.DamagedTransitionReceipt:
    match selection:
        case item_integration.CheckpointSelection():
            return _read_checkpoint_integration_source(work_root, store, facts, selection)
        case item_integration.ProtectedSelection() | item_integration.CompletionSelection():
            match selection:
                case item_integration.ProtectedSelection():
                    context = store.read_candidate_snapshot_context(selection.attempt_id)
                case item_integration.CompletionSelection():
                    context = store.read_completion_candidate_snapshot_context(selection.attempt_id)
                case _ as unreachable:
                    assert_never(unreachable)
            if context is None:
                return _candidate_unavailable(facts, "The retained review candidate has no accepted snapshot bytes.")
            try:
                snapshot = candidate_snapshots.verify_candidate_snapshot_context(
                    context, selection.candidate, read_reference(work_root, context.reference)
                ).snapshot
            except (ArtifactError, ValueError) as error:
                return _integration_evidence_invalid(selection.attempt_id, context.reference, str(error))
            compared_from = _compared_from_revision(snapshot)
            match selection:
                case item_integration.ProtectedSelection():
                    source: item_integration.Source = item_integration.ProtectedReview(
                        str(selection.attempt_id), snapshot.candidate, compared_from
                    )
                case item_integration.CompletionSelection():
                    source = item_integration.Completion(str(selection.attempt_id), snapshot.candidate, compared_from)
                case _ as unreachable:
                    assert_never(unreachable)
            return VerifiedIntegrationSource(snapshot, source)
        case _ as unreachable:
            assert_never(unreachable)


def read_item_integration(
    source_checkout: Path, work_root: Path, store: ports.WorkStore, facts: item_integration.Facts, target: str
) -> item_integration.ItemIntegration | IntegrationFailure | query_models.DamagedTransitionReceipt:
    """Select and verify one accepted diff, then observe its content at a local target tip."""
    selection = item_integration.select_source(facts)
    if isinstance(selection, query_models.DamagedTransitionReceipt):
        return selection
    if isinstance(selection, item_integration.Unavailable):
        return _candidate_unavailable(facts, selection.reason)
    evidence = _read_integration_source(work_root, store, facts, selection)
    if isinstance(evidence, IntegrationFailure | query_models.DamagedTransitionReceipt):
        return evidence
    try:
        observed = root.read_integration_content(source_checkout, target, evidence.snapshot.diff)
    except RootError as error:
        return IntegrationFailure(
            error.code,
            str(error),
            FailureDetails(
                observed=(FailureFact("project_root", str(source_checkout)), FailureFact("diagnostic", str(error))),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
            "Correct the project checkout, then repeat this read.",
        )
    if isinstance(observed, root.UnresolvedTarget):
        return IntegrationFailure(
            item_integration.IntegrationFailureCode.TARGET_UNRESOLVED,
            f"Target '{target}' does not resolve to a local commit.",
            FailureDetails(
                observed=(FailureFact("target", target), FailureFact("project_root", str(source_checkout))),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
            "Name an existing local branch, remote-tracking ref, tag, or full commit; fetch it outside Pinboard first when remote freshness matters.",
        )
    presence = (
        item_integration.Presence.NO_CHANGE
        if not evidence.snapshot.diff
        else item_integration.Presence.CONTENT_PRESENT
        if observed.present
        else item_integration.Presence.CONTENT_NOT_PRESENT
    )
    return item_integration.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        facts.revision,
        str(facts.item_id),
        target,
        observed.target_revision,
        evidence.source,
        presence,
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
