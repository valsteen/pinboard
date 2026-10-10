"""Verify accepted candidate bytes and restore only an exact selected checkout.

Restoration can change the source checkout, never the ledger or authority.
Post-mutation verification failures retain that actual effect and forbid replay.
"""

from collections.abc import Callable
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
    checkpoint_compatibility_models,
    checkpoint_packages,
    ports,
    query_models,
    stored_state,
    work_brief_models,
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


_UNAVAILABLE_MESSAGES = {
    "no-protected-candidate": "The item has no protected candidate and its current attempt has no checkpoint acceptance.",
    "direct-close": "The item closed directly without a completion, so it names no candidate.",
    "checkpoint-package-without-candidate-snapshot": (
        "The item's selected checkpoint acceptance or closing receipt names no accepted candidate snapshot."
    ),
    "retained-pre-snapshot-review-candidate": (
        "The item's candidate was recorded before candidate snapshots, so no accepted snapshot bytes exist."
    ),
}

_UNAVAILABLE_RECOVERY = "Read the item leaf to see which candidate the item currently exposes."

_EVIDENCE_INVALID_RECOVERY = "Run 'pinboard validate' to diagnose the accepted candidate evidence."

_TARGET_UNRESOLVED_RECOVERY = (
    "Name an existing local branch, remote-tracking ref, tag, or full commit; fetch outside Pinboard first "
    "when remote freshness matters."
)

_READ_UNAVAILABLE_RECOVERY = "Correct the source checkout so its Git reads succeed."


class _IntegrationEvidenceError(Exception):
    """The accepted candidate evidence of a selected source failed verification."""


class _IntegrationSnapshotMissing(Exception):
    """The selected checkpoint acceptance names no portable candidate snapshot."""


@dataclass(frozen=True, slots=True)
class ItemIntegrationFailure:
    """One typed reason an integration read named no comparable reviewed candidate."""

    code: str
    message: str
    observed: tuple[FailureFact, ...]
    retry: RetryDisposition
    recovery: str | None


type ItemIntegrationOutcome = query_models.ItemIntegration | ItemIntegrationFailure


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


def _verify_reviewed_snapshot(
    work_root: Path,
    reference: stored_state.ArtifactReference,
    verify: Callable[[bytes], candidate_snapshots.CandidateSnapshot],
) -> candidate_snapshots.CandidateSnapshot:
    try:
        encoded = read_reference(work_root, reference)
        return verify(encoded)
    except (ArtifactError, ValueError, msgspec.DecodeError) as error:
        raise _IntegrationEvidenceError(str(error)) from error


def _verify_checkpoint_snapshot(
    work_root: Path,
    item_id: WorkItemId,
    selected: query_models.CheckpointIntegrationCandidate,
) -> candidate_snapshots.CandidateSnapshot:
    try:
        package_bytes = read_reference(work_root, selected.package_reference)
    except ArtifactError as error:
        raise _IntegrationEvidenceError(str(error)) from error
    package = checkpoint_packages.validate_selected_checkpoint_review_package(
        selected.checkpoint_receipt,
        selected.package_reference,
        package_bytes,
        attempt_id=str(selected.attempt_id),
        item_id=str(item_id),
    )
    if isinstance(package, work_brief_models.WorkBriefFailure):
        raise _IntegrationEvidenceError(package.message)
    if not isinstance(
        package,
        (work_brief_models.CheckpointReviewPackageV3, checkpoint_compatibility_models.CheckpointReviewPackageV2),
    ):
        raise _IntegrationSnapshotMissing()
    identity = package.candidate_snapshot
    if (
        identity.kind,
        identity.key,
        identity.revision,
        identity.selector,
        identity.content_sha256,
        identity.size_bytes,
    ) != (
        selected.snapshot_reference.kind.value,
        selected.snapshot_reference.key,
        selected.snapshot_reference.revision,
        selected.snapshot_reference.selector,
        selected.snapshot_reference.content_sha256,
        selected.snapshot_reference.size_bytes,
    ):
        raise _IntegrationEvidenceError(
            "The checkpoint review package's candidate snapshot identity does not match its accepted artifact reference."
        )
    snapshot = _verify_reviewed_snapshot(
        work_root,
        selected.snapshot_reference,
        candidate_snapshots.decode_candidate_snapshot,
    )
    if (snapshot.attempt_id, snapshot.item_id, snapshot.candidate) != (
        package.attempt_id,
        package.item_id,
        package.candidate,
    ):
        raise _IntegrationEvidenceError("The checkpoint candidate snapshot does not match its accepted review package.")
    return snapshot


def _evidence_invalid_failure(
    attempt_id: AttemptId,
    reference: stored_state.ArtifactReference,
    error: _IntegrationEvidenceError,
) -> ItemIntegrationFailure:
    return ItemIntegrationFailure(
        "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
        str(error),
        (
            FailureFact("attempt_id", str(attempt_id)),
            FailureFact("accepted_reference", f"{reference.key}#{reference.revision}"),
        ),
        RetryDisposition.DO_NOT_RETRY,
        _EVIDENCE_INVALID_RECOVERY,
    )


def _verify_integration_candidate(
    work_root: Path,
    item_id: WorkItemId,
    facts: query_models.ItemIntegrationCandidate,
) -> tuple[query_models.IntegrationSource, candidate_snapshots.CandidateSnapshot, str | None] | ItemIntegrationFailure:
    """Verify one selected candidate's accepted bytes and name its source variant."""
    match facts:
        case query_models.ReviewIntegrationCandidate():
            try:
                snapshot = _verify_reviewed_snapshot(
                    work_root,
                    facts.snapshot_context.reference,
                    lambda encoded: (
                        candidate_snapshots.verify_candidate_snapshot_context(
                            facts.snapshot_context, None, encoded
                        ).snapshot
                    ),
                )
            except _IntegrationEvidenceError as error:
                return _evidence_invalid_failure(facts.attempt_id, facts.snapshot_context.reference, error)
            return "protected-review", snapshot, None
        case query_models.CheckpointIntegrationCandidate():
            try:
                snapshot = _verify_checkpoint_snapshot(work_root, item_id, facts)
            except _IntegrationSnapshotMissing:
                return ItemIntegrationFailure(
                    "INTEGRATION_CANDIDATE_UNAVAILABLE",
                    _UNAVAILABLE_MESSAGES["checkpoint-package-without-candidate-snapshot"],
                    (
                        FailureFact("item_id", str(item_id)),
                        FailureFact("item_state", facts.item_state.value),
                        FailureFact("reason", "checkpoint-package-without-candidate-snapshot"),
                    ),
                    RetryDisposition.CORRECT_INPUT,
                    _UNAVAILABLE_RECOVERY,
                )
            except _IntegrationEvidenceError as error:
                return _evidence_invalid_failure(facts.attempt_id, facts.snapshot_reference, error)
            return "accepted-checkpoint", snapshot, facts.checkpoint_id
        case query_models.CompletionIntegrationCandidate():
            try:
                snapshot = _verify_reviewed_snapshot(
                    work_root,
                    facts.snapshot_context.reference,
                    lambda encoded: (
                        candidate_snapshots.verify_candidate_snapshot_context(
                            facts.snapshot_context, None, encoded
                        ).snapshot
                    ),
                )
            except _IntegrationEvidenceError as error:
                return _evidence_invalid_failure(facts.attempt_id, facts.snapshot_context.reference, error)
            return "completion", snapshot, None
        case _ as unreachable:
            assert_never(unreachable)


def read_item_integration(
    source_checkout: Path,
    work_root: Path,
    store: ports.WorkStore,
    item_id: WorkItemId,
    target: str,
    project_root: str,
) -> ItemIntegrationOutcome:
    """Compare one reviewed candidate's accepted diff with a caller-named target by content.

    Selects the reviewed candidate from the focused ledger facts, verifies its accepted
    snapshot bytes, and composes them with the one Git read that resolves the target and
    reverse-applies the recorded diff against a private temporary index.
    """

    selection = store.read_item_integration_facts(item_id)
    if selection is None:
        return ItemIntegrationFailure(
            DecisionFailureCode.ITEM_NOT_FOUND.value,
            f"Item '{item_id}' was not found.",
            (),
            RetryDisposition.CORRECT_INPUT,
            None,
        )
    checkpoint_id = None
    match selection.facts:
        case query_models.MissingIntegrationCandidate():
            return ItemIntegrationFailure(
                "INTEGRATION_CANDIDATE_UNAVAILABLE",
                _UNAVAILABLE_MESSAGES[selection.facts.reason],
                (
                    FailureFact("item_id", str(item_id)),
                    FailureFact("item_state", selection.facts.item_state.value),
                    FailureFact("reason", selection.facts.reason),
                ),
                RetryDisposition.CORRECT_INPUT,
                _UNAVAILABLE_RECOVERY,
            )
        case _:
            verified = _verify_integration_candidate(work_root, item_id, selection.facts)
    if isinstance(verified, ItemIntegrationFailure):
        return verified
    source, snapshot, checkpoint_id = verified
    attempt_id = selection.facts.attempt_id
    try:
        observation = root.read_integration_content_presence(source_checkout, target, snapshot.diff)
    except RootError as error:
        return ItemIntegrationFailure(
            error.code.value,
            str(error),
            (
                FailureFact("project_root", project_root),
                FailureFact("git_diagnostic", str(error).removeprefix(f"{error.code.value}: ")),
            ),
            RetryDisposition.CORRECT_INPUT,
            _READ_UNAVAILABLE_RECOVERY,
        )
    match observation:
        case root.IntegrationTargetUnresolved():
            return ItemIntegrationFailure(
                "INTEGRATION_TARGET_UNRESOLVED",
                f"Target '{target}' does not resolve to a commit in this checkout.",
                (
                    FailureFact("target", target),
                    FailureFact("project_root", project_root),
                ),
                RetryDisposition.CORRECT_INPUT,
                _TARGET_UNRESOLVED_RECOVERY,
            )
        case root.IntegrationTargetResolved():
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.ItemIntegration(
        "pinboard-item-integration/v1",
        "sqlite-v7",
        str(selection.project_revision),
        str(item_id),
        target,
        observation.target_revision,
        source,
        str(attempt_id),
        snapshot.candidate,
        _compared_from_revision(snapshot),
        observation.presence,
        checkpoint_id,
    )
