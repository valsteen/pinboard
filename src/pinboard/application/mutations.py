from typing import assert_never

from pinboard.application import stored_state
from pinboard.application.artifacts import CheckpointArtifacts, EvidenceArtifactRef, ResultArtifactRef
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointAcceptanceMutation,
    CheckpointArtifactChanges,
    MutationReceipt,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.definition_decisions import DefinitionRevisionDecision
from pinboard.domain.history import HistoryOutcome, encode_transition_receipt_outcome
from pinboard.domain.identifiers import (
    ArtifactRefId,
    HistoryId,
    HistorySubjectId,
    HostId,
    SubjectId,
    TaskId,
)


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case TransitionMutation(decision=decision) | CheckpointAcceptanceMutation(decision=decision):
            checkpoint = None
            candidate = None
            match decision.change:
                case decision_models.CheckpointAcceptanceChange(checkpoint=value, candidate=accepted_candidate):
                    checkpoint = str(value)
                    candidate = str(accepted_candidate)
                case (
                    decision_models.ReviewAcceptanceChange(candidate=accepted_candidate)
                    | decision_models.ReviewSubmissionChange(protected_candidate_after=accepted_candidate)
                ):
                    candidate = str(accepted_candidate)
                case (
                    decision_models.AcceptedProposalChange()
                    | decision_models.ActivationChange()
                    | decision_models.AttemptStateChange()
                    | decision_models.BlockAttemptChange()
                    | decision_models.BlockItemChange()
                    | decision_models.AttemptClosureChange()
                    | decision_models.CompletionChange()
                    | decision_models.ItemClosureChange()
                    | decision_models.ItemStateChange()
                    | decision_models.MergedProposalChange()
                    | decision_models.ReturnedProposalChange()
                    | decision_models.RejectedProposalChange()
                    | decision_models.ResumeAttemptChange()
                    | decision_models.ReviewReturnChange()
                    | DefinitionRevisionDecision()
                ):
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(
                    evidence=decision.receipt.evidence,
                    outcome=decision.receipt.outcome,
                    candidate=candidate,
                    checkpoint=checkpoint,
                ),
            )
        case ProposalCreationMutation() | AttemptAuthorityMutation() | PreparationAuthorityMutation():
            transition = mutation.receipt.transition
            return HistoryOutcome(
                "transition-receipt/v1",
                encode_transition_receipt_outcome(evidence=transition.evidence, outcome=transition.outcome),
            )
        case _ as unreachable:
            assert_never(unreachable)


def stored_transition_receipt(mutation: StoredStateMutation) -> stored_state.StoredTransitionReceipt:
    """Convert one accepted mutation into its exact persisted receipt."""

    outcome = _history_outcome(mutation)
    receipt = mutation.receipt
    return stored_state.StoredTransitionReceipt(
        receipt.history_id,
        receipt.project_revision,
        receipt.transition.action_id,
        receipt.action_kind,
        receipt.subject_id,
        receipt.artifact_ref_id,
        receipt.authorization,
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.input_schema,
        receipt.input_payload,
        outcome.outcome_schema,
        work_models.CanonicalJson(outcome.payload),
        receipt.transition.decided_at,
    )


def _checkpoint_artifact_ids(
    before: stored_state.StoredWorkState,
    artifacts: CheckpointArtifacts,
) -> CheckpointArtifactChanges:
    assigned: list[tuple[work_models.ArtifactKind, str, int, ArtifactRefId, str, str, int]] = [
        (
            value.kind,
            value.key,
            value.revision,
            value.artifact_ref_id,
            value.selector,
            value.content_sha256,
            value.size_bytes,
        )
        for value in before.artifact_references
    ]
    next_id = 1 + max((int(value[3]) for value in assigned), default=0)

    def identify(published: ResultArtifactRef | EvidenceArtifactRef) -> ArtifactRefId:
        nonlocal next_id
        existing = next(
            (value for value in assigned if value[:3] == (published.kind, published.key, published.revision)),
            None,
        )
        if existing is not None:
            return existing[3]
        result = ArtifactRefId(next_id)
        next_id += 1
        assigned.append(
            (
                published.kind,
                published.key,
                published.revision,
                result,
                published.selector,
                published.content_sha256,
                published.size_bytes,
            )
        )
        return result

    result_id = identify(artifacts.result)
    review_id = identify(artifacts.review)
    return CheckpointArtifactChanges(artifacts.result, result_id, artifacts.review, review_id)


def _transition_receipt[SubjectT: SubjectId](
    before: stored_state.StoredWorkState,
    capability: decision_models.MutationActionCapability[SubjectT],
    action_kind: decision_models.ActionKind,
    transition: decision_models.TransitionReceipt,
    artifact_ref_id: ArtifactRefId | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> MutationReceipt:
    if capability.authorization == decision_models.AuthorizationKind.ATTEMPT and capability.lease_id is not None:
        authority = capability.command_authority
        if authority is not None:
            actor_task_id, actor_host_id = authority.task_id, authority.host_id
    elif capability.authorization == decision_models.AuthorizationKind.PREPARATION:
        preparation = capability.preparation_authority
        if preparation is not None:
            actor_task_id, actor_host_id = preparation.task_id, preparation.host_id
    revision = before.lifecycle.project.revision + 1
    return MutationReceipt(
        transition,
        HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
        revision,
        action_kind,
        HistorySubjectId(capability.subject),
        artifact_ref_id,
        capability.authorization,
        actor_task_id,
        actor_host_id,
        "decision/v1",
        work_models.CanonicalJson(b"{}"),
    )


def project_transition_mutation(
    before: stored_state.StoredWorkState,
    decision: decision_models.TransitionDecision,
    actor_task_id: TaskId | None = None,
    actor_host_id: HostId | None = None,
) -> TransitionMutation:
    """Project one accepted non-checkpoint decision into its exact mutation."""

    return TransitionMutation(
        decision,
        _transition_receipt(
            before,
            decision.action.capability,
            decision.action.kind,
            decision.receipt,
            None,
            actor_task_id,
            actor_host_id,
        ),
    )


def project_checkpoint_acceptance_mutation(
    before: stored_state.StoredWorkState,
    decision: decision_models.CheckpointAcceptanceDecision,
    artifacts: CheckpointArtifacts,
    actor_task_id: TaskId | None = None,
    actor_host_id: HostId | None = None,
) -> CheckpointAcceptanceMutation:
    """Project checkpoint acceptance with its exact result and review artifacts."""

    checkpoint_changes = _checkpoint_artifact_ids(before, artifacts)
    return CheckpointAcceptanceMutation(
        decision,
        _transition_receipt(
            before,
            decision.action.capability,
            decision.action.kind,
            decision.receipt,
            checkpoint_changes.review_id,
            actor_task_id,
            actor_host_id,
        ),
        checkpoint_changes,
    )
