"""Verify accepted candidate bytes, restore only an exact selected checkout, or observe integration.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
Integration observation composes verified accepted snapshot bytes with the Git
adapter's read-only presence check and changes nothing.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import candidate_compatibility, root
from pinboard.adapters.files.artifacts import read_reference
from pinboard.adapters.files.errors import ArtifactError, RootError
from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
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
    """An accepted reference the integration source depends on failed verification."""

    attempt_id: AttemptId
    reference: stored_state.ArtifactReference
    defect: str


@dataclass(frozen=True, slots=True)
class CandidateIntegrationObservation:
    target_revision: str
    compared_from_revision: str
    presence: query_models.IntegrationPresence


type CandidateIntegrationResult = (
    CandidateIntegrationObservation
    | IntegrationEvidenceInvalid
    | root.UnresolvedIntegrationTarget
    | query_models.IntegrationCandidateUnavailable
)


def _compared_from_revision(snapshot: candidate_snapshots.CandidateSnapshot) -> str:
    """A working-tree snapshot is compared from its actual preimage; a commit snapshot from its accepted base."""

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


def _checkpoint_candidate_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    source: query_models.AcceptedCheckpointSource,
    unavailable: query_models.IntegrationCandidateUnavailable,
) -> candidate_snapshots.CandidateSnapshot | IntegrationEvidenceInvalid | query_models.IntegrationCandidateUnavailable:
    package_reference = source.package_reference
    try:
        package = work_briefs.decode_canonical_checkpoint_review_package(read_reference(work_root, package_reference))
    except ArtifactError as error:
        return IntegrationEvidenceInvalid(source.attempt_id, package_reference, str(error))
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return IntegrationEvidenceInvalid(source.attempt_id, package_reference, package.message)
    if not isinstance(package, work_brief_models.CheckpointReviewPackageV3):
        # Retained v1 and v2 packages reference raw patch bytes or nothing, never a canonical candidate snapshot.
        return unavailable
    identity = package.candidate_snapshot
    reference = store.read_artifact_reference(work_models.ArtifactKind.EVIDENCE, identity.key, identity.revision)
    if reference is None or reference.content_sha256 != identity.content_sha256:
        return IntegrationEvidenceInvalid(
            source.attempt_id, package_reference, "The package candidate snapshot does not resolve to accepted bytes."
        )
    try:
        snapshot = candidate_snapshots.decode_candidate_snapshot(read_reference(work_root, reference))
    except (ArtifactError, msgspec.DecodeError, ValueError) as error:
        return IntegrationEvidenceInvalid(source.attempt_id, reference, str(error))
    if snapshot.attempt_id != str(source.attempt_id) or snapshot.candidate != source.candidate:
        return IntegrationEvidenceInvalid(
            source.attempt_id, reference, "The candidate snapshot does not match the accepted checkpoint."
        )
    return snapshot


def observe_candidate_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    facts: query_models.ItemIntegrationFacts,
    source: query_models.IntegrationSource,
    target: str,
) -> CandidateIntegrationResult:
    """Resolve the selected source's accepted snapshot bytes, then ask Git whether they reached the target.

    An empty recorded diff is ``no-change`` without the presence read. Git read
    failures other than an unresolvable target remain the adapter's ``RootError``.
    """

    match source:
        case query_models.ProtectedReviewSource() | query_models.CompletionSource():
            context = store.read_candidate_snapshot_context(source.attempt_id)
            if context is None:
                return query_models.IntegrationCandidateUnavailable(
                    facts.work_item_id, facts.state, query_models.IntegrationUnavailableReason.PRE_SNAPSHOT_CANDIDATE
                )
            evidence = read_candidate_evidence_from_context(work_root, context, source.candidate)
            if isinstance(evidence, DecisionFailure):
                return IntegrationEvidenceInvalid(source.attempt_id, context.reference, evidence.message)
            snapshot: candidate_snapshots.CandidateSnapshot = evidence.snapshot
        case query_models.AcceptedCheckpointSource():
            selected = _checkpoint_candidate_snapshot(
                work_root,
                store,
                source,
                query_models.IntegrationCandidateUnavailable(
                    facts.work_item_id, facts.state, query_models.IntegrationUnavailableReason.PACKAGE_WITHOUT_SNAPSHOT
                ),
            )
            if isinstance(selected, (IntegrationEvidenceInvalid, query_models.IntegrationCandidateUnavailable)):
                return selected
            snapshot = selected
        case _ as unreachable:
            assert_never(unreachable)
    target_revision = root.resolve_integration_target(source_checkout, target)
    if isinstance(target_revision, root.UnresolvedIntegrationTarget):
        return target_revision
    compared_from = _compared_from_revision(snapshot)
    if not snapshot.diff:
        return CandidateIntegrationObservation(
            target_revision, compared_from, query_models.IntegrationPresence.NO_CHANGE
        )
    present = root.observe_diff_presence(source_checkout, target_revision, snapshot.diff)
    return CandidateIntegrationObservation(
        target_revision,
        compared_from,
        query_models.IntegrationPresence.CONTENT_PRESENT
        if present
        else query_models.IntegrationPresence.CONTENT_NOT_PRESENT,
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
