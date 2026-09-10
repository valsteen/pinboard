from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from typing import Any  # noqa: TID251 - fixture corruption intentionally crosses the typed boundary

from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionResult
from pinboard.domain.identifiers import (
    AttemptId,
    CandidateId,
    ItemId,
    ProposalId,
    SubjectId,
    TaskId,
)
from pinboard.interfaces.errors import TransitionInputFailure


def replace(instance: Any, **changes: Any) -> Any:  # noqa: ANN401
    """Create valid variants and deliberately malformed values for rejection tests."""
    return dataclass_replace(instance, **changes)


def expect_success[T](value: DecisionResult[T]) -> T:
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
    value: decision_models.TransitionCommand | TransitionInputFailure,
) -> decision_models.TransitionCommand:
    if isinstance(value, TransitionInputFailure):
        raise AssertionError(f"Expected a transition command, received {value.code.value}: {value.message}")
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
        ItemId(item),
        state,
        accepted_scope_revision,
        accepted_scope_digest,
        CandidateId(protected_candidate_revision) if protected_candidate_revision is not None else None,
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


def accept_proposal_input(
    item: str,
    state: work_models.AcceptedProposalState,
    next_action: str,
    timing: work_models.Timing | None = None,
    depends_on: tuple[str, ...] = (),
) -> work_models.AcceptProposalInput:
    return work_models.AcceptProposalInput(
        ItemId(item), state, next_action, timing, tuple(ItemId(value) for value in depends_on)
    )


def defer_input(timing: str, reopen_condition: str) -> work_models.DeferInput:
    return work_models.DeferInput(work_models.Timing(timing), reopen_condition)
