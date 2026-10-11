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
from pinboard.application import candidate_snapshot_compatibility_models, candidate_snapshots, ports, query_models
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


@dataclass(frozen=True, slots=True)
class IntegrationPresenceEvidence:
    presence: query_models.ItemIntegrationPresence
    target_revision: str
    compared_from_revision: str
    source: query_models.ItemIntegrationSource


@dataclass(frozen=True, slots=True)
class IntegrationGitFailure:
    code: RootErrorCode
    diagnostic: str


def read_integration_presence(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    selection: query_models.IntegrationSourceSelection,
    target: str,
) -> IntegrationPresenceEvidence | IntegrationGitFailure | DecisionFailure:
    """Compose verified accepted snapshot bytes with the Git reverse-apply read."""

    snapshot = _load_selected_snapshot(work_root, store, selection)
    if isinstance(snapshot, DecisionFailure):
        return snapshot
    diff = snapshot.diff
    compared_from = (
        snapshot.accepted_base_revision
        if isinstance(
            snapshot,
            (
                candidate_snapshots.CommitCandidateSnapshot,
                candidate_snapshots.DeclaredCommitCandidateSnapshot,
            ),
        )
        else snapshot.preimage_revision
    )
    source = _source_record(selection, compared_from)
    if not diff:
        try:
            resolved = root.resolve_integration_target(source_checkout, target)
        except RootError as error:
            if "not a valid Git revision" in str(error):
                return DecisionFailure(
                    DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
                    f"Integration target '{target}' does not resolve to a commit in {source_checkout}.",
                    FailureDetails(
                        observed=(
                            FailureFact("target", target),
                            FailureFact("project_root", str(source_checkout)),
                        ),
                        mismatches=(),
                        retry=RetryDisposition.CORRECT_INPUT,
                        effect=EffectDisposition.UNCHANGED,
                        changed_surfaces=(),
                        alternatives=(),
                    ),
                )
            return IntegrationGitFailure(error.code, str(error))
        if isinstance(resolved, root.UnresolvedIntegrationTarget):
            return DecisionFailure(
                DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
                f"Integration target '{resolved.target}' does not resolve to a commit in {source_checkout}.",
                FailureDetails(
                    observed=(
                        FailureFact("target", resolved.target),
                        FailureFact("project_root", str(source_checkout)),
                    ),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        return IntegrationPresenceEvidence("no-change", resolved.revision, compared_from, source)
    try:
        observation = root.read_integration_presence(source_checkout, target, diff)
    except RootError as error:
        if "not a valid Git revision" in str(error):
            return DecisionFailure(
                DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
                f"Integration target '{target}' does not resolve to a commit in {source_checkout}.",
                FailureDetails(
                    observed=(
                        FailureFact("target", target),
                        FailureFact("project_root", str(source_checkout)),
                    ),
                    mismatches=(),
                    retry=RetryDisposition.CORRECT_INPUT,
                    effect=EffectDisposition.UNCHANGED,
                    changed_surfaces=(),
                    alternatives=(),
                ),
            )
        return IntegrationGitFailure(error.code, str(error))
    if isinstance(observation, root.UnresolvedIntegrationTarget):
        return DecisionFailure(
            DecisionFailureCode.INTEGRATION_TARGET_UNRESOLVED,
            f"Integration target '{observation.target}' does not resolve to a commit in {source_checkout}.",
            FailureDetails(
                observed=(
                    FailureFact("target", observation.target),
                    FailureFact("project_root", str(source_checkout)),
                ),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return IntegrationPresenceEvidence(
        "content-present" if observation.present else "content-not-present",
        observation.target_revision,
        compared_from,
        source,
    )


def _source_record(
    selection: query_models.IntegrationSourceSelection,
    compared_from: str,
) -> query_models.ItemIntegrationSource:
    match selection.variant:
        case "protected-review":
            return query_models.ProtectedReviewSource(selection.attempt_id, selection.candidate_revision, compared_from)
        case "accepted-checkpoint":
            checkpoint_id = selection.checkpoint_id
            assert checkpoint_id is not None
            return query_models.AcceptedCheckpointSource(
                selection.attempt_id, selection.candidate_revision, compared_from, checkpoint_id
            )
        case "completion":
            return query_models.CompletionSource(selection.attempt_id, selection.candidate_revision, compared_from)
        case _ as unreachable:
            assert_never(unreachable)


def _evidence_invalid(
    attempt_id: str,
    reference_selector: str,
    error: object,
) -> DecisionFailure:
    return DecisionFailure(
        DecisionFailureCode.INTEGRATION_CANDIDATE_EVIDENCE_INVALID,
        f"Candidate snapshot bytes for attempt '{attempt_id}' failed verification: {error}",
        FailureDetails(
            observed=(
                FailureFact("attempt_id", attempt_id),
                FailureFact("accepted_reference", reference_selector),
            ),
            mismatches=(),
            retry=RetryDisposition.DO_NOT_RETRY,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def _load_selected_snapshot(
    work_root: Path,
    store: ports.WorkStore,
    selection: query_models.IntegrationSourceSelection,
) -> DecisionResult[candidate_snapshots.CandidateSnapshot]:
    if selection.snapshot_reference is not None:
        reference = selection.snapshot_reference
        try:
            encoded = read_reference(work_root, reference)
        except (ArtifactError, ValueError, OSError) as error:
            return _evidence_invalid(selection.attempt_id, reference.selector, error)
        try:
            decoded = candidate_snapshots.decode_candidate_snapshot(encoded)
        except (ValueError, msgspec.DecodeError) as error:
            return _evidence_invalid(selection.attempt_id, reference.selector, error)
        return decoded
    context = store.read_candidate_snapshot_context(AttemptId(selection.attempt_id))
    if context is None:
        return DecisionFailure(
            DecisionFailureCode.INTEGRATION_CANDIDATE_UNAVAILABLE,
            f"Item candidate for attempt '{selection.attempt_id}' has no accepted snapshot bytes: "
            "a retained pre-snapshot review candidate.",
            FailureDetails(
                observed=(
                    FailureFact("attempt_id", selection.attempt_id),
                    FailureFact("candidate", selection.candidate_revision),
                ),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    evidence = read_candidate_evidence_from_context(work_root, context, selection.candidate_revision)
    if isinstance(evidence, DecisionFailure):
        return DecisionFailure(
            DecisionFailureCode.INTEGRATION_CANDIDATE_EVIDENCE_INVALID,
            evidence.message,
            FailureDetails(
                observed=(
                    FailureFact("attempt_id", selection.attempt_id),
                    FailureFact("accepted_reference", context.reference.selector),
                ),
                mismatches=(),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    return evidence.snapshot
