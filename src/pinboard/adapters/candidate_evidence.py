"""Verify accepted candidate bytes, compare them with a named target, and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
The integration content check only reads the ledger, accepted artifacts and Git.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError, RootErrorCode
from pinboard.application import (
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


@dataclass(frozen=True, slots=True)
class IntegrationEvidenceInvalid:
    """The selected candidate's accepted evidence failed verification."""

    attempt_id: AttemptId
    reference: stored_state.ArtifactReference
    defect: str


@dataclass(frozen=True, slots=True)
class IntegrationGitUnavailable:
    """The Git content read failed for a reason other than an unresolved target."""

    code: RootErrorCode
    diagnostic: str


type ItemIntegrationObservation = (
    query_models.ItemIntegration
    | root.TargetUnresolved
    | query_models.IntegrationCandidateUnavailable
    | IntegrationEvidenceInvalid
    | IntegrationGitUnavailable
    | query_models.DamagedTransitionReceipt
    | DecisionFailure
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


def _checkpoint_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    selected: query_models.CheckpointSelection,
) -> candidate_snapshots.CandidateSnapshot | IntegrationEvidenceInvalid:
    """Verify a checkpoint package and the candidate snapshot bytes it names."""

    package_reference = selected.package_reference
    try:
        package_bytes = read_reference(work_root, package_reference)
    except ArtifactError as error:
        return IntegrationEvidenceInvalid(selected.attempt_id, package_reference, str(error))
    package = checkpoint_packages.validate_selected_checkpoint_review_package(
        selected.receipt,
        package_reference,
        package_bytes,
        attempt_id=str(selected.attempt_id),
        item_id=str(selected.work_item_id),
    )
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return IntegrationEvidenceInvalid(selected.attempt_id, package_reference, package.message)
    identity = package.candidate_snapshot
    candidate_reference = store.read_artifact_reference(
        work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision
    )
    if candidate_reference is None or not (
        checkpoint_packages.canonical_checkpoint_candidate_reference(package, candidate_reference)
        and checkpoint_packages.portable_candidate_identity_matches(identity, candidate_reference)
    ):
        return IntegrationEvidenceInvalid(
            selected.attempt_id,
            package_reference,
            "The checkpoint package candidate snapshot identity does not resolve to its accepted artifact reference.",
        )
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, candidate_reference))
    except (ArtifactError, msgspec.DecodeError, ValueError) as error:
        return IntegrationEvidenceInvalid(selected.attempt_id, candidate_reference, str(error))
    if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
        str(selected.attempt_id),
        str(selected.work_item_id),
        selected.candidate,
    ):
        return IntegrationEvidenceInvalid(
            selected.attempt_id,
            candidate_reference,
            "The checkpoint candidate snapshot does not match its accepted checkpoint.",
        )
    return snapshot


def _verified_snapshot(
    work_root: Path, context: query_models.CandidateSnapshotContextFacts
) -> candidate_snapshots.CandidateSnapshot | IntegrationEvidenceInvalid:
    evidence = read_candidate_evidence_from_context(work_root, context, context.candidate_revision)
    if isinstance(evidence, DecisionFailure):
        return IntegrationEvidenceInvalid(context.attempt_id, context.reference, evidence.message)
    return evidence.snapshot


def _integration_source(
    work_root: Path,
    store: ports.WorkStore,
    selected: query_models.IntegrationSourceSelection,
) -> tuple[candidate_snapshots.CandidateSnapshot, query_models.IntegrationSource] | IntegrationEvidenceInvalid:
    """Verify the selected candidate's accepted snapshot bytes and name the source they came from."""

    match selected:
        case query_models.ProtectedReviewSelection(snapshot=context):
            snapshot = _verified_snapshot(work_root, context)
            if isinstance(snapshot, IntegrationEvidenceInvalid):
                return snapshot
            return snapshot, query_models.ProtectedReviewIntegrationSource(
                str(context.attempt_id), snapshot.candidate, candidate_snapshots.compared_from_revision(snapshot)
            )
        case query_models.CompletionSelection(snapshot=context):
            snapshot = _verified_snapshot(work_root, context)
            if isinstance(snapshot, IntegrationEvidenceInvalid):
                return snapshot
            return snapshot, query_models.CompletionIntegrationSource(
                str(context.attempt_id), snapshot.candidate, candidate_snapshots.compared_from_revision(snapshot)
            )
        case query_models.CheckpointSelection():
            snapshot = _checkpoint_snapshot(work_root, store, selected)
            if isinstance(snapshot, IntegrationEvidenceInvalid):
                return snapshot
            return snapshot, query_models.AcceptedCheckpointIntegrationSource(
                str(selected.attempt_id),
                selected.checkpoint,
                snapshot.candidate,
                candidate_snapshots.compared_from_revision(snapshot),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _observe_presence(
    source_checkout: Path, target: str, diff: bytes
) -> tuple[str, query_models.IntegrationPresence] | root.TargetUnresolved | IntegrationGitUnavailable:
    """Resolve the target and check the diff there; an empty diff is no-change without a content check."""

    try:
        observed = (
            root.observe_reviewed_diff_at_target(source_checkout, target, diff)
            if diff
            else root.resolve_target_commit(source_checkout, target)
        )
    except RootError as error:
        return IntegrationGitUnavailable(error.code, str(error))
    match observed:
        case root.TargetUnresolved():
            return observed
        case root.ReviewedDiffPresent(target_revision=revision):
            return revision, query_models.IntegrationPresence.CONTENT_PRESENT
        case root.ReviewedDiffAbsent(target_revision=revision):
            return revision, query_models.IntegrationPresence.CONTENT_NOT_PRESENT
        case str():
            return observed, query_models.IntegrationPresence.NO_CHANGE
        case _ as unreachable:
            assert_never(unreachable)


def observe_item_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    work_item_id: WorkItemId,
    target: str,
) -> ItemIntegrationObservation:
    """Report whether an item's reviewed candidate diff is present in a caller-named target's content."""

    facts = store.read_integration_facts(work_item_id)
    if facts is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{work_item_id}' was not found.", None)
    selected = queries.select_integration_source(facts)
    if isinstance(selected, query_models.IntegrationCandidateUnavailable | query_models.DamagedTransitionReceipt):
        return selected
    verified = _integration_source(work_root, store, selected)
    if not isinstance(verified, tuple):
        return verified
    snapshot, source = verified
    presence = _observe_presence(source_checkout, target, snapshot.diff)
    if not isinstance(presence, tuple):
        return presence
    target_revision, content = presence
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        str(facts.project_revision),
        str(facts.work_item_id),
        target,
        target_revision,
        source,
        content,
    )
