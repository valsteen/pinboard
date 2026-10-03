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
    checkpoint_compatibility_models,
    checkpoint_packages,
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
    except (ArtifactError, ValueError) as error:
        return DecisionFailure(DecisionFailureCode.TRANSITION_INPUT_INVALID, str(error), None)


def read_item_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.ItemIntegrationReader,
    item_id: str,
    target: str,
) -> DecisionResult[query_models.ItemIntegration] | query_models.DamagedTransitionReceipt:
    """Compose one focused candidate selection, its accepted bytes, and the local Git content read."""

    selection = queries.select_item_integration_candidate(store, WorkItemId(item_id))
    if isinstance(selection, (DecisionFailure, query_models.DamagedTransitionReceipt)):
        return selection
    evidence = _read_selected_integration_evidence(work_root, store, selection)
    if isinstance(evidence, (DecisionFailure, query_models.DamagedTransitionReceipt)):
        return evidence
    snapshot = evidence.snapshot
    observation = root.observe_candidate_content(source_checkout, target, snapshot.diff)
    match observation:
        case root.IntegrationTargetUnresolved():
            return _integration_failure(
                DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
                f"Integration target '{target}' does not resolve to a commit in the local repository.",
                (FailureFact("target", target), FailureFact("project_root", str(source_checkout))),
                FailureMismatch("target", "existing local branch, remote-tracking ref, tag, or full commit", target),
                RetryDisposition.CORRECT_INPUT,
            )
        case root.CandidateContentPresent(target_revision=target_revision):
            presence = work_models.IntegrationPresence.CONTENT_PRESENT
        case root.CandidateContentNotPresent(target_revision=target_revision):
            presence = work_models.IntegrationPresence.CONTENT_NOT_PRESENT
        case root.CandidateContentNoChange(target_revision=target_revision):
            presence = work_models.IntegrationPresence.NO_CHANGE
        case _ as unreachable:
            assert_never(unreachable)

    source = _integration_source(selection, snapshot)
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        str(selection.project_revision),
        item_id,
        target,
        target_revision,
        source,
        presence,
    )


def _read_selected_integration_evidence(
    work_root: Path,
    store: ports.ItemIntegrationReader,
    selection: query_models.ItemIntegrationCandidateSelection,
) -> DecisionResult[candidate_snapshots.CandidateSnapshotEvidence] | query_models.DamagedTransitionReceipt:
    match selection:
        case query_models.ProtectedReviewCandidateSelection() | query_models.CompletionCandidateSelection():
            evidence = read_candidate_evidence_from_context(
                work_root, selection.snapshot_context, selection.candidate_revision
            )
            if isinstance(evidence, DecisionFailure):
                return _integration_evidence_failure(str(selection.item_id), selection.attempt_id, evidence.message)
            return evidence
        case query_models.AcceptedCheckpointCandidateSelection():
            return _read_checkpoint_candidate_evidence(work_root, store, selection)
        case _ as unreachable:
            assert_never(unreachable)


def _integration_source(
    selection: query_models.ItemIntegrationCandidateSelection,
    snapshot: candidate_snapshots.CandidateSnapshot,
) -> query_models.ItemIntegrationSource:
    match snapshot:
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
        ):
            compared_from = snapshot.preimage_revision
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            compared_from = snapshot.accepted_base_revision
        case _ as unreachable:
            assert_never(unreachable)
    match selection:
        case query_models.ProtectedReviewCandidateSelection():
            return query_models.ProtectedReviewIntegrationSource(
                str(selection.attempt_id), selection.candidate_revision, compared_from
            )
        case query_models.AcceptedCheckpointCandidateSelection():
            return query_models.AcceptedCheckpointIntegrationSource(
                str(selection.attempt_id), selection.candidate_revision, compared_from, selection.checkpoint_id
            )
        case query_models.CompletionCandidateSelection():
            return query_models.CompletionIntegrationSource(
                str(selection.attempt_id), selection.candidate_revision, compared_from
            )
        case _ as unreachable:
            assert_never(unreachable)


def _read_checkpoint_candidate_evidence(
    work_root: Path,
    store: ports.ItemIntegrationReader,
    selection: query_models.AcceptedCheckpointCandidateSelection,
) -> DecisionResult[candidate_snapshots.CandidateSnapshotEvidence]:
    try:
        package_bytes = read_reference(work_root, selection.package_reference)
        package = work_briefs.decode_canonical_checkpoint_review_package(package_bytes)
        if isinstance(package, work_brief_models.WorkBriefFailure):
            return _integration_evidence_failure(str(selection.item_id), selection.attempt_id, package.message)
        validated = checkpoint_packages.validate_selected_checkpoint_review_package(
            selection.receipt,
            selection.package_reference,
            package_bytes,
            attempt_id=str(selection.attempt_id),
            item_id=str(selection.item_id),
        )
        if isinstance(validated, work_brief_models.WorkBriefFailure):
            return _integration_evidence_failure(str(selection.item_id), selection.attempt_id, validated.message)
        match validated:
            case work_brief_models.CheckpointReviewPackageV3():
                identity = validated.candidate_snapshot
            case checkpoint_compatibility_models.CheckpointReviewPackageV2():
                identity = validated.candidate_snapshot
            case _:
                return _integration_unavailable(
                    str(selection.item_id),
                    "The accepted checkpoint predates candidate snapshot references.",
                )
        reference = store.read_artifact_reference(
            work_models.ArtifactKind(identity.kind), identity.key, identity.revision
        )
        if reference is None or (
            reference.selector,
            reference.content_sha256,
            reference.size_bytes,
        ) != (identity.selector, identity.content_sha256, identity.size_bytes):
            return _integration_evidence_failure(
                str(selection.item_id),
                selection.attempt_id,
                "the checkpoint candidate reference does not match storage",
            )
        encoded = read_reference(work_root, reference)
        snapshot = candidate_snapshots.decode_candidate_snapshot(encoded)
        if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
            str(selection.attempt_id),
            str(selection.item_id),
            selection.candidate_revision,
        ):
            return _integration_evidence_failure(
                str(selection.item_id),
                selection.attempt_id,
                "the checkpoint snapshot does not match its accepted candidate",
            )
        return candidate_snapshots.CandidateSnapshotEvidence(snapshot, reference, selection.receipt)
    except (ArtifactError, OSError, ValueError, msgspec.DecodeError) as error:
        return _integration_evidence_failure(str(selection.item_id), selection.attempt_id, str(error))


def _integration_unavailable(item_id: str, reason: str) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.INTEGRATION_CANDIDATE_UNAVAILABLE,
        f"Item '{item_id}' has no reviewed candidate with accepted snapshot bytes: {reason}",
        FailureDetails(
            observed=(FailureFact("item_id", item_id),),
            mismatches=(FailureMismatch("candidate_snapshot", "accepted snapshot reference", reason),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _integration_evidence_failure(item_id: str, attempt_id: AttemptId, reason: str) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.INTEGRATION_CANDIDATE_EVIDENCE_INVALID,
        f"Accepted candidate evidence for attempt '{attempt_id}' on item '{item_id}' is invalid: {reason}",
        FailureDetails(
            observed=(FailureFact("item_id", item_id), FailureFact("attempt_id", str(attempt_id))),
            mismatches=(FailureMismatch("candidate_evidence", "verified accepted snapshot bytes", reason),),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _integration_failure(
    code: DecisionFailureCode,
    message: str,
    observed: tuple[FailureFact, ...],
    mismatch: FailureMismatch,
    retry: RetryDisposition,
) -> DecisionFailure:
    return DecisionFailure(
        code,
        message,
        FailureDetails(
            observed=observed,
            mismatches=(mismatch,),
            retry=retry,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


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
