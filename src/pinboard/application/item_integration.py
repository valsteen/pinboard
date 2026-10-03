"""Select one reviewed content source from focused lifecycle facts.

Presence describes the accepted diff at a local target tip, independently of
the coordinator's repository reconciliation relation.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal

import msgspec

from pinboard.application import action_models, query_models, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId

type Target = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^-\r\n\x00][^\r\n\x00]*\z")]
type FullRevision = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]


class Presence(Enum):
    CONTENT_PRESENT = "content-present"
    CONTENT_NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


class IntegrationFailureCode(Enum):
    TARGET_UNRESOLVED = "INTEGRATION_TARGET_UNRESOLVED"
    CANDIDATE_UNAVAILABLE = "INTEGRATION_CANDIDATE_UNAVAILABLE"
    CANDIDATE_EVIDENCE_INVALID = "INTEGRATION_CANDIDATE_EVIDENCE_INVALID"


class ProtectedReview(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: action_models.NonEmptyLine
    candidate_revision: action_models.NonEmptyLine
    compared_from_revision: FullRevision


class AcceptedCheckpoint(
    msgspec.Struct, tag="accepted-checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: action_models.NonEmptyLine
    candidate_revision: action_models.NonEmptyLine
    compared_from_revision: FullRevision
    checkpoint_id: action_models.NonEmptyLine


class Completion(msgspec.Struct, tag="completion", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    attempt_id: action_models.NonEmptyLine
    candidate_revision: action_models.NonEmptyLine
    compared_from_revision: FullRevision


type Source = ProtectedReview | AcceptedCheckpoint | Completion


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    authority: Literal["sqlite-v7"]
    revision: Annotated[int, msgspec.Meta(ge=0)]
    item_id: action_models.NonEmptyLine
    target: Target
    target_revision: FullRevision
    source: Source
    presence: Presence


@dataclass(frozen=True, slots=True)
class Facts:
    revision: Annotated[int, msgspec.Meta(ge=0)]
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    attempt_id: AttemptId | None
    attempt_state: work_models.AttemptState | None
    candidate: str | None
    closing_receipt: stored_state.StoredTransitionReceipt | None


@dataclass(frozen=True, slots=True)
class ProtectedSelection:
    attempt_id: AttemptId
    candidate: str


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class CompletionSelection:
    attempt_id: AttemptId
    candidate: str


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: str


type Selection = ProtectedSelection | CheckpointSelection | CompletionSelection


def select_source(facts: Facts) -> Selection | Unavailable | query_models.DamagedTransitionReceipt:
    if facts.state == stored_state.StoredWorkItemState.DROPPED:
        return Unavailable("The item was closed directly, without a completion candidate.")
    if facts.state == stored_state.StoredWorkItemState.DONE:
        receipt = facts.closing_receipt
        if receipt is None or receipt.action_kind != decision_models.ActionKind.COMPLETE:
            return Unavailable("The item was closed directly, without a completion candidate.")
        try:
            outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CompletionAcceptanceOutcome)
            if receipt.outcome_schema != "completion-acceptance/v2" or msgspec.json.encode(
                outcome, order="sorted"
            ) != bytes(receipt.outcome_payload):
                raise ValueError("The completion outcome is not canonical.")
        except (msgspec.DecodeError, ValueError) as error:
            return query_models.DamagedTransitionReceipt(
                AttemptId(str(receipt.subject_id)),
                receipt.history_id,
                receipt.committed_at,
                receipt.action_kind,
                str(error),
            )
        return CompletionSelection(AttemptId(str(receipt.subject_id)), outcome.candidate)
    if facts.attempt_id is None:
        return Unavailable("The item has no current attempt or reviewed candidate.")
    if facts.attempt_state == work_models.AttemptState.REVIEW and facts.candidate is not None:
        return ProtectedSelection(facts.attempt_id, facts.candidate)
    return CheckpointSelection(facts.attempt_id)
