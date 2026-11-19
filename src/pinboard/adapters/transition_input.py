"""Decode raw lifecycle payloads or convert typed payloads into exact commands.

Human CLI closure and native lifecycle tools share this representation boundary;
it performs no resource work,
authority selection, legality decision, or persistence effect.
"""

from dataclasses import dataclass
from typing import Literal, assert_never, override

import msgspec

from pinboard.application import action_models as transition_models
from pinboard.application import decision_batches, unblockers
from pinboard.application.amendment_supersessions import supersessions_from_payload
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.errors import DecisionFailureCode, EffectDisposition, FailureDetails, RetryDisposition
from pinboard.domain.identifiers import (
    ArtifactRefId,
    CandidateId,
    CheckpointId,
    HistoryId,
    TaskId,
    WorkItemId,
)

type TransitionInputFailureCode = Literal[
    DecisionFailureCode.ACTION_NOT_MUTATING, DecisionFailureCode.TRANSITION_INPUT_INVALID
]


@dataclass(frozen=True, slots=True)
class TransitionInputFailure:
    code: TransitionInputFailureCode
    message: str
    details: FailureDetails | None

    @override
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


def _names_schema(data: bytes | str | transition_models.InputPayload) -> bool:
    if isinstance(data, msgspec.Struct):
        return isinstance(data, decision_batches.PauseDecisionsInput)
    try:
        return "schema" in msgspec.json.decode(data, type=dict[str, msgspec.Raw])
    except msgspec.DecodeError:
        return False


def _revise_item_input(payload: transition_models.ReviseItemInputPayload) -> work_models.ReviseWorkItemDefinitionInput:
    return work_models.ReviseWorkItemDefinitionInput(
        payload.expected_revision,
        payload.expected_digest,
        TaskId(payload.source_task),
        payload.reason,
        history.work_item_definition_from_payload(payload.definition),
    )


@dataclass(frozen=True, slots=True)
class PendingActivation:
    """An activate action and its decoded input, which the transition adapter resolves against the selected brief."""

    action: decision_models.ActivateAction
    payload: transition_models.ActivateInputPayload


@dataclass(frozen=True, slots=True)
class PendingAmendment:
    """A record-amendment action and its decoded input, which the transition adapter completes with review facts."""

    action: decision_models.RecordAmendmentAction
    payload: transition_models.RecordAmendmentInputPayload


type ParsedTransitionInput = decision_models.TransitionCommand | PendingActivation | PendingAmendment


def parse_close_input(
    action: decision_models.CloseAction,
    data: bytes | str | transition_models.InputPayload,
) -> TransitionInputResult[decision_models.CloseCommand]:
    """Decode close input for its selected close action; the direct human close decodes through it as well."""
    if isinstance(payload := _decode(data, transition_models.CloseInputPayload), TransitionInputFailure):
        return payload
    return decision_models.CloseCommand(
        action, work_models.CloseInput(payload.outcome, payload.reason, payload.human_decision)
    )


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
        case decision_models.ActivateAction():
            if isinstance(payload := _decode(data, transition_models.ActivateInputPayload), TransitionInputFailure):
                return payload
            return PendingActivation(action, payload)
        case decision_models.BlockAttemptAction() | decision_models.BlockWorkItemAction():
            if isinstance(payload := _decode(data, transition_models.BlockInputPayload), TransitionInputFailure):
                return payload
            block_input = work_models.BlockInput(unblockers.unblocker_from_payload(payload.unblocker))
            match action:
                case decision_models.BlockAttemptAction():
                    return decision_models.BlockCommand(action, block_input)
                case decision_models.BlockWorkItemAction():
                    return decision_models.BlockWorkItemCommand(action, block_input)
                case _ as unreachable:
                    assert_never(unreachable)
        case decision_models.CloseAction():
            return parse_close_input(action, data)
        case decision_models.CompleteAction():
            if isinstance(data, msgspec.Struct):
                schema = data.schema if isinstance(data, transition_models.ReviewedCompleteInputPayload) else None
            else:
                try:
                    fields = msgspec.json.decode(data, type=dict[str, msgspec.Raw])
                except msgspec.DecodeError:
                    fields: dict[str, msgspec.Raw] = {}
                schema = (
                    None if (raw_schema := fields.get("schema")) is None else msgspec.json.decode(raw_schema, type=str)
                )
            if schema is not None:
                if isinstance(
                    payload := _decode(data, transition_models.ReviewedCompleteInputPayload), TransitionInputFailure
                ):
                    return payload
                return decision_models.CoveredCompleteCommand(
                    action,
                    work_models.CoveredCompleteInput(
                        payload.human_decision,
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
                        None
                        if payload.landed is msgspec.UNSET
                        else work_models.LandedCompletionInput(
                            payload.landed.target, payload.landed.retain_local_resources
                        ),
                    ),
                )
            if isinstance(payload := _decode(data, transition_models.EvidenceInputPayload), TransitionInputFailure):
                return payload
            return decision_models.CompleteCommand(action, work_models.EvidenceInput(payload.evidence))
        case decision_models.RecordReplacementAction():
            if isinstance(
                payload := _decode(data, transition_models.RecordPlannedReplacementInputPayload),
                TransitionInputFailure,
            ):
                return payload
            return decision_models.RecordReplacementCommand(
                action,
                work_models.RecordPlannedReplacementInput(
                    payload.expected_relation_revision,
                    WorkItemId(payload.replacement_item),
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
                    payload.relation_revision,
                    payload.rationale,
                    payload.accepted_cost,
                    TaskId(payload.recorded_by),
                ),
            )
        case decision_models.PauseAction():
            plain = _decode(data, transition_models.PlainPauseInputPayload)
            if not isinstance(plain, TransitionInputFailure):
                return decision_models.PauseCommand(
                    action,
                    work_models.PlainPauseInput(plain.reason, unblockers.unblocker_from_payload(plain.unblocker)),
                )
            if isinstance(payload := _decode(data, decision_batches.PauseDecisionsInput), TransitionInputFailure):
                # Report the plain-pause failure unless the caller selected the decision-batch shape by its schema.
                return payload if _names_schema(data) else plain
            return decision_models.PauseCommand(action, decision_batches.decisions_from_payload(payload))
        case decision_models.RecordAmendmentAction():
            # The transition adapter adds the review facts it observes for the attempt before deciding.
            if isinstance(
                payload := _decode(data, transition_models.RecordAmendmentInputPayload), TransitionInputFailure
            ):
                return payload
            return PendingAmendment(action, payload)
        case decision_models.ReturnForCorrectionAction():
            if isinstance(payload := _decode(data, transition_models.ReasonInputPayload), TransitionInputFailure):
                return payload
            return decision_models.ReturnForCorrectionCommand(action, work_models.ReasonInput(payload.reason))
        case decision_models.RejectProposalAction():
            if isinstance(
                payload := _decode(data, transition_models.RejectProposalInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.RejectProposalCommand(action, work_models.ReasonInput(payload.reason))
        case decision_models.MergeProposalAction():
            if isinstance(
                payload := _decode(data, transition_models.MergeProposalInputPayload), TransitionInputFailure
            ):
                return payload
            return decision_models.MergeProposalCommand(
                action, work_models.MergeProposalInput(WorkItemId(payload.target))
            )
        case decision_models.ResumeAction():
            plain = _decode(data, transition_models.ResumeInputPayload)
            if isinstance(plain, TransitionInputFailure):
                if isinstance(payload := _decode(data, decision_batches.ResolveDecisionsInput), TransitionInputFailure):
                    return payload
                return decision_models.ResumeCommand(
                    action,
                    work_models.ResolveDecisionsInput(
                        None if payload.brief_artifact_ref_id is None else ArtifactRefId(payload.brief_artifact_ref_id),
                        decision_batches.resolutions_from_payload(payload.resolutions),
                        supersessions_from_payload(payload.supersessions),
                    ),
                )
            return decision_models.ResumeCommand(
                action,
                work_models.ResumeInput(
                    None if plain.brief_artifact_ref_id is None else ArtifactRefId(plain.brief_artifact_ref_id),
                    supersessions_from_payload(plain.supersessions),
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
                    payload.branch,
                    payload.base_revision,
                    ArtifactRefId(payload.brief_artifact_ref_id),
                    supersessions_from_payload(payload.supersessions),
                ),
            )
        case decision_models.ReviseWorkItemAction():
            if isinstance(payload := _decode(data, transition_models.ReviseItemInputPayload), TransitionInputFailure):
                return payload
            return decision_models.ReviseWorkItemCommand(action, _revise_item_input(payload))
        case decision_models.SubmitReviewAction():
            payload = _decode(data, transition_models.SubmitReviewInputPayload)
            if isinstance(payload, TransitionInputFailure):
                declared = _decode(data, transition_models.DeclaredSubmitReviewInputPayload)
                if isinstance(declared, TransitionInputFailure):
                    return declared
                return decision_models.SubmitReviewCommand(
                    action,
                    work_models.DeclaredSubmitReviewInput(
                        CandidateId(declared.candidate), declared.excluded_untracked_paths
                    ),
                )
            return decision_models.SubmitReviewCommand(
                action, work_models.SubmitReviewInput(CandidateId(payload.candidate))
            )
        case (
            decision_models.CancelAction()
            | decision_models.ContinueAction()
            | decision_models.DispatchAction()
            | decision_models.InspectAction()
            | decision_models.PrepareAction()
            | decision_models.CreateProposalAction()
            | decision_models.PreviewParallelAction()
            | decision_models.OrderAction()
            | decision_models.InspectInitiativeAction()
            | decision_models.ReviewCandidateAction()
            | decision_models.ConsolidateAction()
            | decision_models.ReportBlockerAction()
        ):
            return TransitionInputFailure(
                DecisionFailureCode.ACTION_NOT_MUTATING,
                f"Action '{action.kind.value}' is not a canonical transition.",
                None,
            )
        case _ as unreachable:
            assert_never(unreachable)
