"""Select one reviewed change for a focused, caller-named integration observation."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n]+\z")]


class ContentPresence(Enum):
    PRESENT = "content-present"
    NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


class ProtectedReview(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine


class AcceptedCheckpoint(
    msgspec.Struct, tag="accepted-checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine
    checkpoint_id: NonEmptyLine


class Completion(msgspec.Struct, tag="completion", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine


type IntegrationSource = ProtectedReview | AcceptedCheckpoint | Completion


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    item_id: NonEmptyLine
    target: NonEmptyLine
    target_revision: Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
    source: IntegrationSource
    presence: ContentPresence


class ItemFacts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    subject_revision: int


class AttemptFacts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class IntegrationFacts:
    item: ItemFacts
    attempt: AttemptFacts | None
    closing_receipt: stored_state.StoredTransitionReceipt | None
    checkpoint_receipt: stored_state.StoredTransitionReceipt | None
    reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class CandidateUnavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class ProtectedSelection:
    attempt: AttemptFacts
    reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class CompletionSelection:
    attempt: AttemptFacts
    reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    attempt: AttemptFacts
    reference: stored_state.ArtifactReference
    candidate: str
    checkpoint_id: str


type CandidateSelection = ProtectedSelection | CompletionSelection | CheckpointSelection


def select_candidate(
    facts: IntegrationFacts,
) -> CandidateSelection | CandidateUnavailable | query_models.DamagedTransitionReceipt:
    attempt = facts.attempt
    reference = facts.reference
    if facts.item.state == stored_state.StoredWorkItemState.DONE:
        receipt = facts.closing_receipt
        if receipt is None or receipt.action_kind != decision_models.ActionKind.COMPLETE:
            return CandidateUnavailable("The item was closed directly, without a completion candidate.")
        if attempt is None or reference is None:
            return CandidateUnavailable("The retained completion candidate has no accepted snapshot bytes.")
        return CompletionSelection(attempt, reference)
    if attempt is None:
        return CandidateUnavailable("The item has no current attempt with a reviewed candidate.")
    if attempt.state == work_models.AttemptState.REVIEW and attempt.candidate_revision is not None:
        if reference is None:
            return CandidateUnavailable("The retained pre-snapshot review candidate has no accepted snapshot bytes.")
        return ProtectedSelection(attempt, reference)
    receipt = facts.checkpoint_receipt
    if receipt is None:
        return CandidateUnavailable("The current attempt has no protected candidate and no checkpoint acceptance.")
    try:
        outcome = msgspec.json.decode(bytes(receipt.outcome_payload), type=history.CheckpointAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return query_models.DamagedTransitionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            str(error),
        )
    if reference is None:
        return CandidateUnavailable("The checkpoint acceptance has no candidate snapshot package reference.")
    return CheckpointSelection(attempt, reference, outcome.candidate, outcome.checkpoint)
