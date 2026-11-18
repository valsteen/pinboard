from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from typing import Any  # noqa: TID251 - fixture corruption intentionally crosses the typed boundary

from pinboard.adapters.transition_input import ParsedTransitionInput, TransitionInputFailure
from pinboard.application import action_models
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    ProposalId,
    SubjectId,
    TaskId,
    WorkItemId,
)

TEST_HOLD = work_models.WaitingForOutsideCondition("The test fixture's outside condition holds.")


def held_unblocker(state: work_models.WorkState) -> work_models.Unblocker | None:
    """Give held fixture items the one unblocker every paused or blocked item records."""

    return TEST_HOLD if state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED} else None


def plain_pause(reason: str) -> work_models.PlainPauseInput:
    """A plain pause that waits for the fixture's outside condition."""

    return work_models.PlainPauseInput(reason, TEST_HOLD)


def replace(instance: Any, **changes: Any) -> Any:  # noqa: ANN401
    """Create valid variants and deliberately malformed values for rejection tests."""
    return dataclass_replace(instance, **changes)


def expect_success[T, Code: DecisionFailureCode](value: DecisionResult[T, Code]) -> T:
    if isinstance(value, DecisionFailure):
        raise AssertionError(f"Expected success, received {value.code.value}: {value.message}")
    return value


def action[SubjectT: SubjectId, ActionT](
    constructor: Callable[[decision_models.MutationActionCapability[SubjectT]], ActionT],
    subject: SubjectT,
) -> ActionT:
    return constructor(
        decision_models.MutationActionCapability(
            subject=subject,
            label="test action",
            subject_revision="1",
        )
    )


def expect_transition_command(
    value: ParsedTransitionInput | TransitionInputFailure,
) -> decision_models.TransitionCommand:
    if isinstance(value, TransitionInputFailure):
        raise AssertionError(f"Expected a transition command, received {value.code.value}: {value.message}")
    if isinstance(value, action_models.ActivateInputPayload):
        raise AssertionError("Expected a domain command, received an unresolved activation request.")
    if isinstance(value, action_models.RecordAmendmentInputPayload):
        raise AssertionError("Expected a domain command, received an unobserved amendment request.")
    return value


def attempt_record(
    attempt: str,
    item: str,
    state: work_models.AttemptState,
    accepted_scope_revision: int | None = None,
    accepted_scope_digest: str | None = None,
    protected_candidate_revision: str | None = None,
) -> work_models.AttemptRecord:
    return work_models.AttemptRecord(
        AttemptId(attempt),
        WorkItemId(item),
        state,
        accepted_scope_revision,
        accepted_scope_digest,
        CandidateId(protected_candidate_revision) if protected_candidate_revision is not None else None,
        pause_reason=None,
        pending_decision_batch=None,
    )


def proposal_record(proposal: str, revision: str) -> work_models.ProposalRecord:
    return work_models.ProposalRecord(
        ProposalId(proposal),
        revision,
        datetime(2026, 1, 1, tzinfo=UTC),
        TaskId("proposal-source"),
        "Proposal",
        "A proposal was recorded.",
        "The proposal remains relevant.",
        work_models.IndependentProposalRelation(),
        "Preserve the proposal.",
        "The proposal can be evaluated.",
        "No immediate scheduling effect.",
    )
