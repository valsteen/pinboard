from typing import assert_never

import msgspec

from pinboard.application import stored_state
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    CompletionArtifacts,
    EvidenceArtifactRef,
    ResultArtifactRef,
)
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    CheckpointAcceptanceMutation,
    CheckpointArtifactChanges,
    CheckpointMutationAllocation,
    CompletionAcceptanceMutation,
    MutationAllocation,
    MutationReceipt,
    PreparationAuthorityMutation,
    ProposalCreationMutation,
    StoredStateMutation,
    TransitionMutation,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.definition_decisions import DefinitionRevisionDecision
from pinboard.domain.history import (
    HistoryOutcome,
    encode_checkpoint_acceptance_outcome,
    encode_completion_acceptance_outcome,
    encode_transition_receipt_outcome,
)
from pinboard.domain.identifiers import (
    ArtifactRefId,
    HistorySubjectId,
    HostId,
    SubjectId,
    TaskId,
)


def _history_outcome(mutation: StoredStateMutation) -> HistoryOutcome:
    match mutation:
        case CompletionAcceptanceMutation(decision=decision):
            return HistoryOutcome(
                "completion-acceptance/v2",
                encode_completion_acceptance_outcome(
                    candidate=str(decision.change.candidate), evidence=decision.change.evidence
                ),
            )
        case CheckpointAcceptanceMutation(decision=decision):
            evidence = decision.receipt.evidence
            if evidence is None:
                raise AssertionError("Checkpoint acceptance requires evidence.")
            return HistoryOutcome(
                "checkpoint-acceptance/v2",
                encode_checkpoint_acceptance_outcome(
                    candidate=str(decision.change.candidate),
                    checkpoint=str(decision.change.checkpoint),
                    evidence=evidence,
                    outcome=decision.receipt.outcome,
                ),
            )
        case TransitionMutation(decision=decision):
            candidate = None
            match decision.change:
                case (
                    decision_models.ReviewAcceptanceChange(candidate=accepted_candidate)
                    | decision_models.ReviewSubmissionChange(protected_candidate_after=accepted_candidate)
                    | decision_models.ReviewReturnChange(candidate=accepted_candidate)
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
                    | decision_models.RebindAttemptChange()
                    | decision_models.ResumeAttemptChange()
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
    allocation: CheckpointMutationAllocation,
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
        for value in allocation.accepted_artifacts
    ]
    next_id = int(allocation.next_artifact_ref_id)

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
    package_id = identify(artifacts.package)
    return CheckpointArtifactChanges(
        artifacts.result,
        result_id,
        artifacts.review,
        review_id,
        artifacts.package,
        package_id,
    )


def _transition_receipt[SubjectT: SubjectId](
    allocation: MutationAllocation,
    capability: decision_models.MutationActionCapability[SubjectT],
    action_kind: decision_models.ActionKind,
    transition: decision_models.TransitionReceipt,
    artifact_ref_id: ArtifactRefId | None,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
    input_schema: str,
    input_payload: work_models.CanonicalJson,
) -> MutationReceipt:
    if capability.authorization == decision_models.AuthorizationKind.ATTEMPT and capability.lease_id is not None:
        authority = capability.command_authority
        if authority is not None:
            actor_task_id, actor_host_id = authority.task_id, authority.host_id
    elif capability.authorization == decision_models.AuthorizationKind.PREPARATION:
        preparation = capability.preparation_authority
        if preparation is not None:
            actor_task_id, actor_host_id = preparation.task_id, preparation.host_id
    revision = allocation.project_revision + 1
    return MutationReceipt(
        transition,
        allocation.next_history_id,
        revision,
        action_kind,
        HistorySubjectId(capability.subject),
        artifact_ref_id,
        capability.authorization,
        actor_task_id,
        actor_host_id,
        input_schema,
        input_payload,
    )


def project_transition_mutation(
    allocation: MutationAllocation,
    decision: decision_models.TransitionDecision,
    actor_task_id: TaskId | None = None,
    actor_host_id: HostId | None = None,
) -> TransitionMutation:
    """Project one accepted non-checkpoint decision into its exact mutation."""

    input_schema = "decision/v1"
    input_payload = work_models.CanonicalJson(b"{}")
    if decision.action.kind == decision_models.ActionKind.RETURN_FOR_CORRECTION:
        if decision.receipt.evidence is None:
            raise AssertionError("Returning for correction requires a reason.")
        input_schema = "return-for-correction/v1"
        input_payload = work_models.CanonicalJson(
            msgspec.json.encode({"reason": decision.receipt.evidence}, order="sorted")
        )
    return TransitionMutation(
        decision,
        _transition_receipt(
            allocation,
            decision.action.capability,
            decision.action.kind,
            decision.receipt,
            None,
            actor_task_id,
            actor_host_id,
            input_schema,
            input_payload,
        ),
    )


def project_checkpoint_acceptance_mutation(
    allocation: CheckpointMutationAllocation,
    decision: decision_models.CheckpointAcceptanceDecision,
    artifacts: CheckpointArtifacts,
    actor_task_id: TaskId | None = None,
    actor_host_id: HostId | None = None,
) -> CheckpointAcceptanceMutation:
    """Project checkpoint acceptance with its exact result and review artifacts."""

    checkpoint_changes = _checkpoint_artifact_ids(allocation, artifacts)
    return CheckpointAcceptanceMutation(
        decision,
        _transition_receipt(
            allocation,
            decision.action.capability,
            decision.action.kind,
            decision.receipt,
            checkpoint_changes.package_id,
            actor_task_id,
            actor_host_id,
            "decision/v1",
            work_models.CanonicalJson(b"{}"),
        ),
        checkpoint_changes,
    )


def project_completion_acceptance_mutation(
    allocation: CheckpointMutationAllocation,
    decision: decision_models.CompletionAcceptanceDecision,
    value: work_models.CoveredCompleteInput,
    artifacts: CompletionArtifacts,
    actor_task_id: TaskId | None,
    actor_host_id: HostId | None,
) -> CompletionAcceptanceMutation:
    changes = _checkpoint_artifact_ids(
        allocation,
        CheckpointArtifacts(artifacts.result, artifacts.review, artifacts.package),
    )
    input_payload = work_models.CanonicalJson(
        msgspec.json.encode(
            {
                "schema": "pinboard-covered-completion/v1",
                "candidate": str(value.candidate),
                "evidence": value.evidence,
                "reviewer_task_id": str(value.reviewer_task_id),
                "result_sha256": value.result_sha256,
                "review_sha256": value.review_sha256,
                "packages": [
                    {
                        "history_id": int(row.history_id),
                        "package_sha256": row.package_sha256,
                        "disposition": row.disposition.value,
                        "evidence": row.evidence,
                    }
                    for row in value.packages
                ],
            },
            order="sorted",
        )
    )
    return CompletionAcceptanceMutation(
        decision,
        _transition_receipt(
            allocation,
            decision.action.capability,
            decision.action.kind,
            decision.receipt,
            changes.package_id,
            actor_task_id,
            actor_host_id,
            "pinboard-covered-completion/v1",
            input_payload,
        ),
        changes,
    )
