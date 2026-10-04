"""Select one item's retained candidate, without inferring repository relations."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId

type Revision = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00\r\n]+\z")]
type IntegrationTarget = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00\r\n-][^\x00\r\n]*\z")]


class IntegrationPresence(Enum):
    CONTENT_PRESENT = "content-present"
    CONTENT_NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


class ProtectedReviewSource(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: NonEmptyLine


class AcceptedCheckpointSource(
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


type IntegrationSource = ProtectedReviewSource | AcceptedCheckpointSource | CompletionSource


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    item_id: NonEmptyLine
    target: IntegrationTarget
    target_revision: Revision
    source: IntegrationSource
    presence: IntegrationPresence


class IntegrationItemFacts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    subject_revision: int


class IntegrationAttemptFacts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: WorkItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class IntegrationFacts:
    item: IntegrationItemFacts
    attempt: IntegrationAttemptFacts | None
    closing_receipt: query_models.ConsumedTransitionReceipt | None
    checkpoint_receipt: query_models.ConsumedTransitionReceipt | None
    snapshot_reference: stored_state.ArtifactReference | None
    checkpoint_package_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class ProtectedReviewChoice:
    attempt: IntegrationAttemptFacts
    reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class AcceptedCheckpointChoice:
    attempt: IntegrationAttemptFacts
    reference: stored_state.ArtifactReference
    candidate: str
    checkpoint_id: str


@dataclass(frozen=True, slots=True)
class CompletionChoice:
    attempt: IntegrationAttemptFacts
    reference: stored_state.ArtifactReference


type IntegrationChoice = ProtectedReviewChoice | AcceptedCheckpointChoice | CompletionChoice


@dataclass(frozen=True, slots=True)
class CandidateUnavailable:
    reason: str


@dataclass(frozen=True, slots=True)
class DamagedCompletionReceipt:
    attempt_id: AttemptId
    history_id: HistoryId
    committed_at: datetime
    action_kind: Literal[decision_models.ActionKind.COMPLETE]
    defect: str


def _select_completion(facts: IntegrationFacts) -> CompletionChoice | CandidateUnavailable | DamagedCompletionReceipt:
    attempt = facts.attempt
    receipt = facts.closing_receipt
    if receipt is None or receipt.action_kind != decision_models.ActionKind.COMPLETE:
        return CandidateUnavailable("The item was closed directly and has no completion candidate.")
    if attempt is None or attempt.candidate_revision is None:
        return CandidateUnavailable("The completion has no retained candidate.")
    try:
        outcome = msgspec.json.decode(
            receipt.outcome_json.encode(), type=history.CompletionAcceptanceOutcome, strict=True
        )
    except msgspec.DecodeError as error:
        return DamagedCompletionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.COMPLETE,
            str(error),
        )
    if receipt.outcome_schema != "completion-acceptance/v2" or outcome.candidate != attempt.candidate_revision:
        return DamagedCompletionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.COMPLETE,
            "The completion outcome does not match its retained candidate.",
        )
    if facts.snapshot_reference is None:
        return CandidateUnavailable("The retained completion candidate predates accepted snapshot bytes.")
    return CompletionChoice(attempt, facts.snapshot_reference)


def select_integration_candidate(
    facts: IntegrationFacts,
) -> IntegrationChoice | CandidateUnavailable | query_models.DamagedTransitionReceipt | DamagedCompletionReceipt:
    attempt = facts.attempt
    if facts.item.state == stored_state.StoredWorkItemState.DONE:
        return _select_completion(facts)
    if attempt is None:
        return CandidateUnavailable("The item has no current attempt or reviewed candidate.")
    if attempt.state == work_models.AttemptState.REVIEW and attempt.candidate_revision is not None:
        if facts.snapshot_reference is None:
            return CandidateUnavailable("The protected review candidate predates accepted snapshot bytes.")
        return ProtectedReviewChoice(attempt, facts.snapshot_reference)
    receipt = facts.checkpoint_receipt
    if receipt is None:
        return CandidateUnavailable("The current attempt has no protected candidate and no checkpoint acceptance.")
    try:
        outcome = msgspec.json.decode(
            receipt.outcome_json.encode(), type=history.CheckpointAcceptanceOutcome, strict=True
        )
    except msgspec.DecodeError as error:
        return query_models.DamagedTransitionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            str(error),
        )
    if receipt.outcome_schema != "checkpoint-acceptance/v2":
        return CandidateUnavailable("The checkpoint acceptance has no current candidate snapshot reference.")
    if facts.checkpoint_package_reference is None:
        return CandidateUnavailable("The checkpoint acceptance has no candidate snapshot package reference.")
    return AcceptedCheckpointChoice(attempt, facts.checkpoint_package_reference, outcome.candidate, outcome.checkpoint)


def present_integration_source(choice: IntegrationChoice, candidate: str, compared_from: str) -> IntegrationSource:
    match choice:
        case ProtectedReviewChoice():
            return ProtectedReviewSource(str(choice.attempt.attempt_id), candidate, compared_from)
        case AcceptedCheckpointChoice():
            return AcceptedCheckpointSource(
                str(choice.attempt.attempt_id), candidate, compared_from, choice.checkpoint_id
            )
        case CompletionChoice():
            return CompletionSource(str(choice.attempt.attempt_id), candidate, compared_from)
        case _ as unreachable:
            assert_never(unreachable)
