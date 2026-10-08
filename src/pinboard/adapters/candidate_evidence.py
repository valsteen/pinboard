"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

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
from pinboard.application.ports import WorkStoreError
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
class IntegrationFailure:
    """A typed unchanged integration rejection; the MCP boundary renders it with its next step."""

    code: str
    message: str
    details: FailureDetails
    recovery: str


@dataclass(frozen=True, slots=True)
class IntegrationCandidate:
    source: query_models.IntegrationSource
    diff: bytes


def _compared_from(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
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


def _unavailable(item_id: WorkItemId, state: str, reason: str) -> IntegrationFailure:
    return IntegrationFailure(
        "INTEGRATION_CANDIDATE_UNAVAILABLE",
        f"Item '{item_id}' in state '{state}' has no reviewed candidate with accepted snapshot bytes: {reason}.",
        FailureDetails(
            observed=(
                FailureFact("item_id", str(item_id)),
                FailureFact("item_state", state),
                FailureFact("reason", reason),
            ),
            mismatches=(),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
        "Read this item with pinboard_item_status operation item; no integration check applies until a reviewed candidate exists.",
    )


def _invalid(attempt_id: AttemptId, reference_key: str, reason: str) -> IntegrationFailure:
    return IntegrationFailure(
        "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
        f"Candidate evidence for attempt '{attempt_id}' failed verification: {reason}",
        FailureDetails(
            observed=(FailureFact("attempt_id", str(attempt_id)), FailureFact("accepted_reference", reference_key)),
            mismatches=(FailureMismatch("candidate evidence", "verified accepted snapshot", reason),),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
        "Diagnose the named accepted reference with pinboard validate; this call does not repair it.",
    )


def _read_attempt_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    item_id: WorkItemId,
    state: str,
    attempt_id: AttemptId,
    kind: Literal["protected-review", "completion"],
) -> IntegrationCandidate | IntegrationFailure:
    try:
        context = store.read_candidate_snapshot_context(attempt_id)
    except WorkStoreError as error:
        if error.retryable:
            raise
        return _invalid(attempt_id, "candidate snapshot", str(error))
    if context is None:
        return _unavailable(item_id, state, "the attempt retains no accepted snapshot for its candidate")
    reference_key = context.reference.key
    try:
        encoded = read_reference(work_root, context.reference)
        evidence = candidate_snapshots.verify_candidate_snapshot_context(context, None, encoded)
    except (ArtifactError, ValueError) as error:
        return _invalid(attempt_id, reference_key, str(error))
    snapshot = evidence.snapshot
    source = _attempt_source(kind, snapshot)
    return IntegrationCandidate(source, snapshot.diff)


def _attempt_source(
    kind: Literal["protected-review", "completion"],
    snapshot: candidate_snapshots.CandidateSnapshot,
) -> query_models.IntegrationSource:
    attempt_id = snapshot.attempt_id
    match kind:
        case "protected-review":
            return query_models.ProtectedReviewSource(attempt_id, snapshot.candidate, _compared_from(snapshot))
        case "completion":
            return query_models.CompletionSource(attempt_id, snapshot.candidate, _compared_from(snapshot))
        case _ as unreachable:
            assert_never(unreachable)


def _read_package_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    item_id: WorkItemId,
    attempt_id: AttemptId,
    package: work_brief_models.CheckpointReviewPackageV3,
) -> candidate_snapshots.CandidateSnapshot | IntegrationFailure:
    """Read the accepted candidate snapshot that a checkpoint package names and verify its binding."""

    identity = package.candidate_snapshot
    try:
        reference = store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision)
        if reference is None or reference.content_sha256 != identity.content_sha256:
            return _invalid(attempt_id, identity.key, "the checkpoint snapshot reference is not accepted")
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
    except (ArtifactError, ValueError) as error:
        return _invalid(attempt_id, identity.key, str(error))
    except WorkStoreError as error:
        if error.retryable:
            raise
        return _invalid(attempt_id, identity.key, str(error))
    if snapshot.candidate != package.candidate or snapshot.attempt_id != attempt_id or snapshot.item_id != item_id:
        return _invalid(attempt_id, identity.key, "the checkpoint snapshot does not match its package")
    return snapshot


def _read_checkpoint_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    item_id: WorkItemId,
    state: str,
    attempt_id: AttemptId,
) -> IntegrationCandidate | IntegrationFailure:
    try:
        acceptance = store.read_latest_checkpoint_acceptance(attempt_id)
    except WorkStoreError as error:
        if error.retryable:
            raise
        return _invalid(attempt_id, "checkpoint acceptance", str(error))
    if acceptance is None:
        return _unavailable(
            item_id,
            state,
            "the current attempt has no protected candidate and no checkpoint acceptance, or was closed directly",
        )
    if acceptance.package_reference is None:
        return _unavailable(item_id, state, "the accepted checkpoint package has no candidate snapshot reference")
    package_key = acceptance.package_reference.key
    try:
        package = work_briefs.decode_checkpoint_review_package(read_reference(work_root, acceptance.package_reference))
    except ArtifactError as error:
        return _invalid(attempt_id, package_key, str(error))
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return _invalid(attempt_id, package_key, package.message)
    if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        return _unavailable(item_id, state, "the accepted checkpoint package has no candidate snapshot reference")
    snapshot = _read_package_snapshot(work_root, store, item_id, attempt_id, package)
    if isinstance(snapshot, IntegrationFailure):
        return snapshot
    source = query_models.AcceptedCheckpointSource(
        attempt_id, snapshot.candidate, _compared_from(snapshot), acceptance.checkpoint_id
    )
    return IntegrationCandidate(source, snapshot.diff)


def read_integration_candidate(
    work_root: Path,
    store: ports.WorkStore,
    facts: query_models.ItemStatusFacts,
) -> IntegrationCandidate | IntegrationFailure:
    """Select the item's reviewed candidate and return its verified recorded diff with its source."""

    item_id = facts.work_item.work_item_id
    state = facts.work_item.state.value
    selection = queries.select_integration_source(facts)
    match selection:
        case query_models.ProtectedReviewSelection(attempt_id=attempt_id):
            return _read_attempt_snapshot(work_root, store, item_id, state, attempt_id, "protected-review")
        case query_models.AcceptedCheckpointSelection(attempt_id=attempt_id):
            return _read_checkpoint_snapshot(work_root, store, item_id, state, attempt_id)
        case query_models.CompletionSelection(attempt_id=attempt_id):
            return _read_attempt_snapshot(work_root, store, item_id, state, attempt_id, "completion")
        case query_models.IntegrationCandidateUnavailable(reason=reason):
            return _unavailable(item_id, state, reason)
        case _ as unreachable:
            assert_never(unreachable)


def observe_integration_target(
    source_checkout: Path,
    target: str,
    candidate: IntegrationCandidate,
) -> root.TargetContentObservation:
    """Compare the candidate's recorded diff with the target's current local commit."""

    return root.observe_target_content(source_checkout, target, candidate.diff)
