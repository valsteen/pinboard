"""Strict pause/resume receipt records and their domain conversions.

Human words and receipt actors are attribution within Pinboard's trusted-local boundary.
These conversions perform no reads or writes.
"""

from datetime import datetime
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.history import RecordText
from pinboard.domain.identifiers import AttemptId, DecisionId, HistoryId, WorkItemId

type Identity = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]


class PauseDecision(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    decision_id: Identity
    question: RecordText
    options: Annotated[tuple[RecordText, ...], msgspec.Meta(min_length=1)]
    recommendation: RecordText
    unblocks: RecordText
    deferral_cost: RecordText

    def __post_init__(self) -> None:
        if len(set(self.options)) != len(self.options):
            raise ValueError(f"Decision '{self.decision_id}' options must be unique.")
        if self.recommendation not in self.options:
            raise ValueError(f"Decision '{self.decision_id}' recommendation must name one option.")


class PauseDecisionsInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-pause-decisions/v1"]
    reason: RecordText
    decisions: Annotated[tuple[PauseDecision, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        identities = tuple(decision.decision_id for decision in self.decisions)
        if len(set(identities)) != len(identities):
            raise ValueError("Pause decision IDs must be unique.")


class DecisionAnswer(msgspec.Struct, tag="answer", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    decision_id: Identity
    human_decision: RecordText


class DecisionDeferral(msgspec.Struct, tag="defer", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    decision_id: Identity
    human_decision: RecordText
    reopen_condition: RecordText


type DecisionResolution = DecisionAnswer | DecisionDeferral


def _require_unique_resolutions(resolutions: tuple[DecisionResolution, ...]) -> None:
    identities = tuple(resolution.decision_id for resolution in resolutions)
    if len(set(identities)) != len(identities):
        raise ValueError("Resolution decision IDs must be unique.")


class ResolveDecisionsInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-resolve-pause-decisions/v1"]
    brief_artifact_ref_id: PositiveInt | None
    resolutions: Annotated[tuple[DecisionResolution, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        _require_unique_resolutions(self.resolutions)


class ResolvedDecisionsReceipt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-resolved-pause-decisions/v1"]
    pause_history_id: PositiveInt
    resolutions: Annotated[tuple[DecisionResolution, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        _require_unique_resolutions(self.resolutions)


class ResolutionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    resume_history_id: PositiveInt
    resolved_at: datetime
    actor_task_id: str | None
    actor_host_id: str | None
    resolutions: tuple[DecisionResolution, ...]


class DecisionBatchView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pause_history_id: PositiveInt
    paused_at: datetime
    actor_task_id: str | None
    actor_host_id: str | None
    reason: RecordText
    decisions: tuple[PauseDecision, ...]
    resolution: ResolutionView | None


def decisions_from_payload(payload: PauseDecisionsInput) -> work_models.DecisionBatchInput:
    return work_models.DecisionBatchInput(
        payload.reason,
        tuple(
            work_models.PauseDecision(
                DecisionId(decision.decision_id),
                decision.question,
                decision.options,
                decision.recommendation,
                decision.unblocks,
                decision.deferral_cost,
            )
            for decision in payload.decisions
        ),
    )


def resolutions_from_payload(resolutions: tuple[DecisionResolution, ...]) -> tuple[work_models.DecisionResolution, ...]:
    converted: list[work_models.DecisionResolution] = []
    for resolution in resolutions:
        match resolution:
            case DecisionAnswer(decision_id=identity, human_decision=words):
                converted.append(work_models.DecisionAnswer(DecisionId(identity), words))
            case DecisionDeferral(decision_id=identity, human_decision=words, reopen_condition=condition):
                converted.append(work_models.DecisionDeferral(DecisionId(identity), words, condition))
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(converted)


def pause_payload(value: work_models.DecisionBatchInput) -> PauseDecisionsInput:
    return PauseDecisionsInput(
        "pinboard-pause-decisions/v1",
        value.reason,
        tuple(
            PauseDecision(
                str(decision.decision_id),
                decision.question,
                decision.options,
                decision.recommendation,
                decision.unblocks,
                decision.deferral_cost,
            )
            for decision in value.decisions
        ),
    )


def resolution_payload(value: work_models.ResolvedPauseDecisions) -> ResolvedDecisionsReceipt:
    resolutions: list[DecisionResolution] = []
    for resolution in value.resolutions:
        match resolution:
            case work_models.DecisionAnswer(decision_id=identity, human_decision=words):
                resolutions.append(DecisionAnswer(str(identity), words))
            case work_models.DecisionDeferral(decision_id=identity, human_decision=words, reopen_condition=condition):
                resolutions.append(DecisionDeferral(str(identity), words, condition))
            case _ as unreachable:
                assert_never(unreachable)
    return ResolvedDecisionsReceipt(
        "pinboard-resolved-pause-decisions/v1", int(value.pause_history_id), tuple(resolutions)
    )


def pending_batch(attempt_id: AttemptId, view: DecisionBatchView) -> work_models.PendingDecisionBatch | None:
    if view.resolution is not None:
        return None
    return work_models.PendingDecisionBatch(
        attempt_id,
        HistoryId(view.pause_history_id),
        tuple(
            work_models.PauseDecision(
                DecisionId(row.decision_id),
                row.question,
                row.options,
                row.recommendation,
                row.unblocks,
                row.deferral_cost,
            )
            for row in view.decisions
        ),
    )


def project_batch(
    reference: stored_state.DecisionBatchReference,
    pause: stored_state.StoredTransitionReceipt,
    resume: stored_state.StoredTransitionReceipt | None,
    item_id: WorkItemId,
) -> DecisionBatchView:
    """Decode and correlate persisted receipt facts; invalid stored relationships raise ValueError."""
    if (
        pause.history_id != reference.pause_history_id
        or pause.subject_id != reference.attempt_id
        or pause.action_kind != decision_models.ActionKind.PAUSE
        or pause.input_schema != "pinboard-pause-decisions/v1"
    ):
        raise ValueError(f"Decision batch for '{reference.attempt_id}' does not name its structured pause receipt.")
    payload = msgspec.json.decode(bytes(pause.input_payload), type=PauseDecisionsInput)
    if msgspec.json.encode(payload, order="sorted") != bytes(pause.input_payload):
        raise ValueError(f"Decision batch pause receipt {pause.history_id} is not canonical.")
    resolution = None
    if reference.resume_history_id is not None:
        if (
            resume is None
            or resume.history_id != reference.resume_history_id
            or resume.subject_id != item_id
            or resume.action_kind != decision_models.ActionKind.RESUME
            or resume.input_schema != "pinboard-resolved-pause-decisions/v1"
            or resume.history_id <= pause.history_id
        ):
            raise ValueError(f"Decision batch for '{reference.attempt_id}' does not name its resolving resume receipt.")
        resolved = msgspec.json.decode(bytes(resume.input_payload), type=ResolvedDecisionsReceipt)
        if (
            resolved.pause_history_id != pause.history_id
            or {row.decision_id for row in resolved.resolutions} != {row.decision_id for row in payload.decisions}
            or msgspec.json.encode(resolved, order="sorted") != bytes(resume.input_payload)
        ):
            raise ValueError(f"Resume receipt {resume.history_id} does not resolve exactly pause {pause.history_id}.")
        resolution = ResolutionView(
            int(resume.history_id),
            resume.committed_at,
            resume.actor_task_id,
            resume.actor_host_id,
            resolved.resolutions,
        )
    elif resume is not None:
        raise ValueError("A pending decision batch cannot name a resume receipt.")
    return DecisionBatchView(
        int(pause.history_id),
        pause.committed_at,
        pause.actor_task_id,
        pause.actor_host_id,
        payload.reason,
        payload.decisions,
        resolution,
    )


def batches_from_state(state: stored_state.StoredWorkState) -> dict[AttemptId, DecisionBatchView]:
    receipts = {receipt.history_id: receipt for receipt in state.transition_receipts}
    items = {attempt.attempt_id: attempt.item_id for attempt in state.lifecycle.attempts}
    return {
        reference.attempt_id: project_batch(
            reference,
            receipts[reference.pause_history_id],
            None if reference.resume_history_id is None else receipts[reference.resume_history_id],
            items[reference.attempt_id],
        )
        for reference in state.lifecycle.decision_batch_references
    }


def validate_batch_history(
    lifecycle: stored_state.LifecycleRecords, receipts: tuple[stored_state.StoredTransitionReceipt, ...]
) -> None:
    """Validate complete retained receipt history against its latest-reference projection; defects raise ValueError."""
    attempts = {attempt.attempt_id: attempt for attempt in lifecycle.attempts}
    pauses: dict[HistoryId, stored_state.StoredTransitionReceipt] = {}
    expected: dict[AttemptId, stored_state.DecisionBatchReference] = {}
    for receipt in receipts:
        if receipt.input_schema == "pinboard-pause-decisions/v1":
            attempt_id = AttemptId(receipt.subject_id)
            attempt = attempts[attempt_id]
            reference = stored_state.DecisionBatchReference(attempt_id, receipt.history_id, None)
            project_batch(reference, receipt, None, attempt.item_id)
            if (previous := expected.get(attempt_id)) is not None and previous.resume_history_id is None:
                raise ValueError(f"Attempt '{attempt_id}' replaced an unresolved decision batch.")
            pauses[receipt.history_id] = receipt
            expected[attempt_id] = reference
        elif receipt.input_schema == "pinboard-resolved-pause-decisions/v1":
            resolved = msgspec.json.decode(bytes(receipt.input_payload), type=ResolvedDecisionsReceipt)
            pause = pauses[HistoryId(resolved.pause_history_id)]
            attempt_id = AttemptId(pause.subject_id)
            previous = expected[attempt_id]
            if previous.pause_history_id != pause.history_id or previous.resume_history_id is not None:
                raise ValueError(f"Resume receipt {receipt.history_id} does not resolve the current pending batch.")
            reference = stored_state.DecisionBatchReference(attempt_id, pause.history_id, receipt.history_id)
            project_batch(reference, pause, receipt, attempts[attempt_id].item_id)
            expected[attempt_id] = reference
    actual = {reference.attempt_id: reference for reference in lifecycle.decision_batch_references}
    if len(actual) != len(lifecycle.decision_batch_references) or actual != expected:
        raise ValueError("Latest decision-batch references do not match retained pause and resume receipts.")
