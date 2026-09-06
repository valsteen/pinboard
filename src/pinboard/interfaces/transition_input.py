from typing import Final, assert_never

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    ItemId,
    TaskId,
)
from pinboard.interfaces import transition_models
from pinboard.interfaces.errors import TransitionInputFailure, TransitionInputResult


def _input_model_or_none(kind: decision_models.ActionKind) -> transition_models.InputModel | None:  # noqa: C901, PLR0912
    match kind:
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return transition_models.AcceptCheckpointInputPayload
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return transition_models.AcceptReviewAndContinueInputPayload
        case decision_models.ActionKind.ACCEPT_PROPOSAL:
            return transition_models.AcceptProposalInputPayload
        case decision_models.ActionKind.ACTIVATE:
            return transition_models.StoredActivateInputPayload
        case decision_models.ActionKind.BLOCK | decision_models.ActionKind.BLOCK_ITEM:
            return transition_models.BlockInputPayload
        case decision_models.ActionKind.CLOSE:
            return transition_models.CloseInputPayload
        case decision_models.ActionKind.COMPLETE | decision_models.ActionKind.REOPEN:
            return transition_models.EvidenceInputPayload
        case decision_models.ActionKind.DEFER:
            return transition_models.DeferInputPayload
        case (
            decision_models.ActionKind.MARK_READY
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETURN_PROPOSAL
        ):
            return transition_models.ReasonInputPayload
        case decision_models.ActionKind.MERGE_PROPOSAL:
            return transition_models.MergeProposalInputPayload
        case decision_models.ActionKind.RESUME:
            return transition_models.ResumeInputPayload
        case decision_models.ActionKind.REBIND_ATTEMPT:
            return transition_models.RebindAttemptInputPayload
        case decision_models.ActionKind.REVISE_ITEM:
            return transition_models.ReviseItemInputPayload
        case decision_models.ActionKind.SUBMIT_REVIEW:
            return transition_models.SubmitReviewInputPayload
        case (
            decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.REPORT_BLOCKER
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


INPUT_CONTRACT_ACTION_KINDS: Final = tuple(kind.value for kind in decision_models.ActionKind)


def _input_model(kind: decision_models.ActionKind) -> TransitionInputResult[transition_models.InputModel]:
    model = _input_model_or_none(kind)
    if model is None:
        return TransitionInputFailure(
            DecisionFailureCode.ACTION_NOT_MUTATING,
            f"Action '{kind.value}' is not a canonical transition.",
        )
    return model


def _decode[PayloadT: transition_models.InputPayload](
    data: bytes | str,
    model: type[PayloadT],
) -> TransitionInputResult[PayloadT]:
    try:
        return msgspec.json.decode(data, type=model)
    except msgspec.DecodeError as error:
        return TransitionInputFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode transition JSON: {error}",
        )


def _revise_item_input(payload: transition_models.ReviseItemInputPayload) -> work_models.ReviseItemDefinitionInput:
    definition = payload.definition
    return work_models.ReviseItemDefinitionInput(
        ItemId(payload.item_id),
        payload.expected_revision,
        payload.expected_digest,
        TaskId(payload.source_task),
        payload.reason,
        work_models.WorkItemDefinition(
            definition.title,
            definition.objective,
            definition.hypothesis,
            definition.evidence,
            definition.scope,
            definition.non_scope,
            definition.acceptance_criteria,
            tuple(ItemId(value) for value in definition.dependencies),
            definition.effect,
            definition.unlock,
        ),
    )


def parse_item_revision_input(data: bytes | str) -> TransitionInputResult[work_models.ReviseItemDefinitionInput]:
    if isinstance(payload := _decode(data, transition_models.ReviseItemInputPayload), TransitionInputFailure):
        return payload
    return _revise_item_input(payload)


def parse_transition_command(  # noqa: C901, PLR0912, PLR0915 - one visible exhaustive action-to-command boundary
    action: decision_models.Action,
    data: bytes | str,
) -> TransitionInputResult[decision_models.TransitionCommand]:
    match action:
        case decision_models.AcceptCheckpointAction():
            if isinstance(
                payload := _decode(data, transition_models.AcceptCheckpointInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.AcceptCheckpointCommand(
                action,
                work_models.AcceptCheckpointInput(
                    CheckpointId(payload.checkpoint), CandidateId(payload.candidate), payload.evidence
                ),
            )
        case decision_models.AcceptReviewAndContinueAction():
            if isinstance(
                payload := _decode(data, transition_models.AcceptReviewAndContinueInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.AcceptReviewAndContinueCommand(
                action, work_models.AcceptReviewAndContinueInput(CandidateId(payload.candidate), payload.evidence)
            )
        case decision_models.AcceptProposalAction():
            if isinstance(
                payload := _decode(data, transition_models.AcceptProposalInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.AcceptProposalCommand(
                action,
                work_models.AcceptProposalInput(
                    ItemId(payload.item),
                    payload.state,
                    payload.next_action,
                    payload.timing,
                    tuple(ItemId(value) for value in payload.depends_on),
                ),
            )
        case decision_models.ActivateAction():
            if isinstance(
                payload := _decode(data, transition_models.StoredActivateInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.ActivateCommand(
                action,
                work_models.ActivateInput(
                    AttemptId(payload.attempt),
                    payload.branch,
                    payload.base_revision,
                    payload.owner,
                    ArtifactRefId(payload.brief_artifact_ref_id),
                ),
            )
        case decision_models.BlockAttemptAction() | decision_models.BlockItemAction():
            if isinstance(payload := _decode(data, transition_models.BlockInputPayload), TransitionInputFailure):
                return payload
            block_input = work_models.BlockInput(payload.reason, tuple(ItemId(value) for value in payload.depends_on))
            match action:
                case decision_models.BlockAttemptAction():
                    return decision_models.BlockCommand(action, block_input)
                case decision_models.BlockItemAction():
                    return decision_models.BlockItemCommand(action, block_input)
                case _ as unreachable:
                    assert_never(unreachable)
        case decision_models.CloseAction():
            if isinstance(payload := _decode(data, transition_models.CloseInputPayload), TransitionInputFailure):
                return payload
            return decision_models.CloseCommand(action, work_models.CloseInput(payload.outcome, payload.reason))
        case decision_models.CompleteAction() | decision_models.ReopenAction():
            if isinstance(payload := _decode(data, transition_models.EvidenceInputPayload), TransitionInputFailure):
                return payload
            evidence_input = work_models.EvidenceInput(payload.evidence)
            match action:
                case decision_models.CompleteAction():
                    return decision_models.CompleteCommand(action, evidence_input)
                case decision_models.ReopenAction():
                    return decision_models.ReopenCommand(action, evidence_input)
                case _ as unreachable:
                    assert_never(unreachable)
        case decision_models.DeferAction():
            if isinstance(payload := _decode(data, transition_models.DeferInputPayload), TransitionInputFailure):
                return payload
            return decision_models.DeferCommand(
                action, work_models.DeferInput(payload.timing, payload.reopen_condition)
            )
        case (
            decision_models.MarkReadyAction()
            | decision_models.PauseAction()
            | decision_models.RejectProposalAction()
            | decision_models.ReturnForCorrectionAction()
            | decision_models.ReturnProposalAction()
        ):
            if isinstance(payload := _decode(data, transition_models.ReasonInputPayload), TransitionInputFailure):
                return payload
            match action:
                case decision_models.MarkReadyAction():
                    return decision_models.MarkReadyCommand(action, work_models.ReasonInput(payload.reason))
                case decision_models.PauseAction():
                    return decision_models.PauseCommand(action, work_models.ReasonInput(payload.reason))
                case decision_models.RejectProposalAction():
                    return decision_models.RejectProposalCommand(action, work_models.ReasonInput(payload.reason))
                case decision_models.ReturnForCorrectionAction():
                    return decision_models.ReturnForCorrectionCommand(action, work_models.ReasonInput(payload.reason))
                case decision_models.ReturnProposalAction():
                    return decision_models.ReturnProposalCommand(action, work_models.ReasonInput(payload.reason))
                case _ as unreachable:
                    assert_never(unreachable)
        case decision_models.MergeProposalAction():
            if isinstance(
                payload := _decode(data, transition_models.MergeProposalInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.MergeProposalCommand(action, work_models.MergeProposalInput(ItemId(payload.target)))
        case decision_models.ResumeAction():
            if isinstance(payload := _decode(data, transition_models.ResumeInputPayload), TransitionInputFailure):
                return payload
            return decision_models.ResumeCommand(
                action,
                work_models.ResumeInput(
                    None if payload.brief_artifact_ref_id is None else ArtifactRefId(payload.brief_artifact_ref_id)
                ),
            )
        case decision_models.RebindAttemptAction():
            if isinstance(
                payload := _decode(data, transition_models.RebindAttemptInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.RebindAttemptCommand(
                action,
                work_models.RebindAttemptInput(
                    AttemptId(payload.attempt),
                    payload.branch,
                    payload.base_revision,
                    ArtifactRefId(payload.brief_artifact_ref_id),
                ),
            )
        case decision_models.ReviseItemAction():
            if isinstance(payload := _decode(data, transition_models.ReviseItemInputPayload), TransitionInputFailure):
                return payload
            return decision_models.ReviseItemCommand(action, _revise_item_input(payload))
        case decision_models.SubmitReviewAction():
            if isinstance(payload := _decode(data, transition_models.SubmitReviewInputPayload), TransitionInputFailure):
                return payload
            return decision_models.SubmitReviewCommand(
                action, work_models.SubmitReviewInput(CandidateId(payload.candidate))
            )
        case (
            decision_models.ContinueAction()
            | decision_models.DispatchAction()
            | decision_models.InspectAction()
            | decision_models.ReportBlockerAction()
        ):
            return TransitionInputFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                f"Action '{action.kind.value}' is not a canonical transition.",
            )
        case _ as unreachable:
            assert_never(unreachable)


def encoded_transition_input_schema(kind: decision_models.ActionKind) -> TransitionInputResult[bytes]:
    model = _input_model(kind)
    if isinstance(model, TransitionInputFailure):
        return model
    return msgspec.json.encode(msgspec.json.schema(model), order="sorted")
