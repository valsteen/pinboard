"""Select the candidate whose accepted content a status read can compare."""

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import AttemptId


@dataclass(frozen=True, slots=True)
class CurrentAttempt:
    attempt_id: AttemptId
    state: work_models.AttemptState
    candidate: str | None


@dataclass(frozen=True, slots=True)
class ProtectedReview:
    attempt_id: AttemptId
    candidate: str


@dataclass(frozen=True, slots=True)
class AcceptedCheckpoint:
    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class Completion:
    attempt_id: AttemptId
    candidate: str


@dataclass(frozen=True, slots=True)
class CandidateUnavailable:
    reason: str


type SourceSelection = ProtectedReview | AcceptedCheckpoint | Completion | CandidateUnavailable


@dataclass(frozen=True, slots=True)
class UnavailableContext:
    revision: int
    state: stored_state.StoredWorkItemState
    reason: str


@dataclass(frozen=True, slots=True)
class SnapshotContext:
    revision: int
    state: stored_state.StoredWorkItemState
    source: ProtectedReview | Completion
    snapshot: query_models.CandidateSnapshotContextFacts | None


@dataclass(frozen=True, slots=True)
class CheckpointContext:
    revision: int
    state: stored_state.StoredWorkItemState
    source: AcceptedCheckpoint
    receipt: stored_state.StoredTransitionReceipt | None
    package: stored_state.ArtifactReference | None
    candidate: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class InvalidSnapshotContext:
    revision: int
    state: stored_state.StoredWorkItemState
    attempt_id: AttemptId
    candidate: str
    reason: str


@dataclass(frozen=True, slots=True)
class DamagedReceiptContext:
    revision: int
    state: stored_state.StoredWorkItemState
    damage: query_models.DamagedTransitionReceipt


type IntegrationContext = (
    UnavailableContext | SnapshotContext | CheckpointContext | InvalidSnapshotContext | DamagedReceiptContext
)


class Presence(Enum):
    PRESENT = "content-present"
    NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n\x00\u2028\u2029]+\z")]
type CommitRevision = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]


class ProtectedReviewSource(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine


class CheckpointSource(
    msgspec.Struct, tag="accepted-checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine
    checkpoint_id: NonEmptyLine


class CompletionSource(msgspec.Struct, tag="completion", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    revision: NonEmptyLine
    item_id: NonEmptyLine
    target: NonEmptyLine
    target_revision: CommitRevision
    source: ProtectedReviewSource | CheckpointSource | CompletionSource
    presence: Presence


def select_source(
    state: stored_state.StoredWorkItemState,
    current_attempt: CurrentAttempt | None,
    closure: query_models.ItemClosureFacts | None,
) -> SourceSelection:
    match state:
        case stored_state.StoredWorkItemState.DONE:
            if closure is None or closure.action_kind != decision_models.ActionKind.COMPLETE:
                return CandidateUnavailable("The item was closed directly, without a completion candidate.")
            closing = closure.closing_attempt
            if closing is None or closing.candidate_revision is None:
                return CandidateUnavailable("The completion has no retained candidate.")
            return Completion(closing.attempt_id, closing.candidate_revision)
        case stored_state.StoredWorkItemState.SUPERSEDED | stored_state.StoredWorkItemState.DROPPED:
            return CandidateUnavailable("The item was closed directly, without a completion candidate.")
        case (
            stored_state.StoredWorkItemState.INTAKE
            | stored_state.StoredWorkItemState.READY
            | stored_state.StoredWorkItemState.ACTIVE
            | stored_state.StoredWorkItemState.PAUSED
            | stored_state.StoredWorkItemState.BLOCKED
            | stored_state.StoredWorkItemState.DEFERRED
            | stored_state.StoredWorkItemState.REVIEW
        ):
            if current_attempt is None:
                return CandidateUnavailable("The item has no current attempt with a reviewed candidate.")
            if current_attempt.state == work_models.AttemptState.REVIEW and current_attempt.candidate is not None:
                return ProtectedReview(current_attempt.attempt_id, current_attempt.candidate)
            return AcceptedCheckpoint(current_attempt.attempt_id)
        case _ as unreachable:
            assert_never(unreachable)
