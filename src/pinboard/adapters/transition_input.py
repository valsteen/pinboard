"""Decode raw lifecycle payloads or convert typed payloads into exact commands.

Human CLI closure and native lifecycle tools share this representation boundary;
it performs no resource work,
authority selection, legality decision, or persistence effect.
"""

from dataclasses import dataclass
from typing import assert_never

import msgspec

from pinboard.application import action_models as transition_models
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailureCode, EffectDisposition, FailureDetails, RetryDisposition
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HistoryId,
    ItemId,
    TaskId,
)


@dataclass(frozen=True, slots=True)
class TransitionInputFailure:
    code: DecisionFailureCode
    message: str
    details: FailureDetails | None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


type TransitionInputResult[T] = T | TransitionInputFailure


def _decode[PayloadT: transition_models.InputPayload](
    data: bytes | str | transition_models.InputPayload,
    model: type[PayloadT],
) -> TransitionInputResult[PayloadT]:
    try:
        if isinstance(data, model):
            return data
        if isinstance(data, msgspec.Struct):
            raise ValueError(f"Expected {model.__name__}, not {type(data).__name__}.")
        return msgspec.json.decode(data, type=model)
    except (msgspec.DecodeError, ValueError) as error:
        return TransitionInputFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot decode transition JSON: {error}",
            FailureDetails(
                observed=(),
                mismatches=(),
                retry=RetryDisposition.CORRECT_INPUT,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
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
            work_models.CheckoutPolicy(definition.checkout_policy),
            tuple(
                work_models.WorkObligation(
                    work_models.ObligationId(obligation.obligation_id),
                    obligation.statement,
                    work_models.ObligationDeferralPolicy(obligation.deferral_policy),
                )
                for obligation in definition.obligations
            ),
        ),
    )


type ParsedTransitionInput = decision_models.TransitionCommand | transition_models.ActivateInputPayload


def parse_transition_input(  # noqa: C901, PLR0912, PLR0915 - one visible exhaustive action-to-input boundary
    action: decision_models.Action,
    data: bytes | str | transition_models.InputPayload,
) -> TransitionInputResult[ParsedTransitionInput]:
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
            if isinstance(payload := _decode(data, transition_models.ActivateInputPayload), TransitionInputFailure):
                return payload
            return payload
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
        case decision_models.CompleteAction():
            if isinstance(data, msgspec.Struct):
                covered = isinstance(data, transition_models.CoveredCompleteInputPayload)
            else:
                try:
                    fields = msgspec.json.decode(data, type=dict[str, msgspec.Raw])
                except msgspec.DecodeError:
                    fields: dict[str, msgspec.Raw] = {}
                covered = "schema" in fields
            if covered:
                if isinstance(
                    payload := _decode(data, transition_models.CoveredCompleteInputPayload), TransitionInputFailure
                ):
                    return payload
                return decision_models.CoveredCompleteCommand(
                    action,
                    work_models.CoveredCompleteInput(
                        CandidateId(payload.candidate),
                        payload.evidence,
                        TaskId(payload.reviewer_task_id),
                        payload.result_sha256,
                        payload.review_sha256,
                        tuple(
                            work_models.CoveredCompletionPackageInput(
                                HistoryId(row.history_id),
                                row.package_sha256,
                                work_models.CompletionPackageDisposition(row.disposition),
                                row.evidence,
                            )
                            for row in payload.packages
                        ),
                    ),
                )
            if isinstance(payload := _decode(data, transition_models.EvidenceInputPayload), TransitionInputFailure):
                return payload
            return decision_models.CompleteCommand(action, work_models.EvidenceInput(payload.evidence))
        case decision_models.ReopenAction():
            if isinstance(payload := _decode(data, transition_models.EvidenceInputPayload), TransitionInputFailure):
                return payload
            return decision_models.ReopenCommand(action, work_models.EvidenceInput(payload.evidence))
        case decision_models.RecordReplacementAction():
            if isinstance(
                payload := _decode(data, transition_models.RecordPlannedReplacementInputPayload),
                TransitionInputFailure,
            ):
                return payload
            return decision_models.RecordReplacementCommand(
                action,
                work_models.RecordPlannedReplacementInput(
                    ItemId(payload.affected_item),
                    payload.expected_relation_revision,
                    ItemId(payload.replacement_item),
                    payload.replacement_cost,
                    payload.status,
                    TaskId(payload.recorded_by),
                ),
            )
        case decision_models.RetainTemporarilyAction():
            if isinstance(
                payload := _decode(data, transition_models.RetainTemporarilyInputPayload),
                TransitionInputFailure,
            ):
                return payload
            return decision_models.RetainTemporarilyCommand(
                action,
                work_models.RetainTemporarilyInput(
                    ItemId(payload.affected_item),
                    payload.relation_revision,
                    payload.rationale,
                    payload.accepted_cost,
                    TaskId(payload.recorded_by),
                ),
            )
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
                None,
            )
        case _ as unreachable:
            assert_never(unreachable)
