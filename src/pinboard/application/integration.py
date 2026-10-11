"""Select the reviewed candidate for a focused integration-content observation."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n]+\z")]
type CommitRevision = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]


class Presence(Enum):
    CONTENT_PRESENT = "content-present"
    CONTENT_NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


class ProtectedReviewSource(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: CommitRevision


class AcceptedCheckpointSource(
    msgspec.Struct, tag="accepted-checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: CommitRevision
    checkpoint_id: NonEmptyLine


class CompletionSource(msgspec.Struct, tag="completion", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    attempt_id: NonEmptyLine
    candidate_revision: NonEmptyLine
    compared_from_revision: CommitRevision


type Source = ProtectedReviewSource | AcceptedCheckpointSource | CompletionSource


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    item_id: NonEmptyLine
    target: NonEmptyLine
    target_revision: CommitRevision
    source: Source
    presence: Presence


class ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    subject_revision: int


class AttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    state: work_models.AttemptState
    candidate_revision: str | None
    candidate_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class Facts:
    item: ItemRow
    attempt: AttemptRow | None
    closing_receipt: query_models.ConsumedTransitionReceipt | None
    checkpoint_receipt: query_models.ConsumedTransitionReceipt | None
    snapshot_reference: stored_state.ArtifactReference | None
    package_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class Unavailable:
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    reason: str


@dataclass(frozen=True, slots=True)
class InvalidEvidence:
    attempt_id: AttemptId
    reference: stored_state.ArtifactReference | None
    reason: str


@dataclass(frozen=True, slots=True)
class ProtectedReview:
    attempt_id: AttemptId
    candidate: str
    reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class AcceptedCheckpoint:
    attempt_id: AttemptId
    candidate: str
    checkpoint_id: str
    package_reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class Completion:
    attempt_id: AttemptId
    candidate: str
    reference: stored_state.ArtifactReference


type Selection = ProtectedReview | AcceptedCheckpoint | Completion


def _completion_source(
    facts: Facts,
) -> Completion | Unavailable | InvalidEvidence | query_models.DamagedTransitionReceipt:
    attempt = facts.attempt
    closing = facts.closing_receipt
    if closing is None or closing.action_kind != decision_models.ActionKind.COMPLETE or attempt is None:
        return Unavailable(
            facts.item.item_id, facts.item.state, "The item closed directly, without a completion candidate."
        )
    try:
        match closing.outcome_schema:
            case "completion-acceptance/v2":
                outcome = msgspec.json.decode(closing.outcome_json, type=history.CompletionAcceptanceOutcome)
                candidate = outcome.candidate
            case "transition-receipt/v1":
                candidate = msgspec.json.decode(closing.outcome_json, type=history.TransitionReceiptOutcome).candidate
            case _:
                candidate = None
    except msgspec.DecodeError as error:
        return query_models.DamagedTransitionReceipt(
            attempt.attempt_id,
            closing.history_id,
            closing.committed_at,
            decision_models.ActionKind.COMPLETE,
            str(error),
        )
    if candidate is None or facts.snapshot_reference is None:
        return Unavailable(
            facts.item.item_id,
            facts.item.state,
            "The retained completion candidate has no accepted snapshot bytes.",
        )
    if candidate != attempt.candidate_revision:
        return InvalidEvidence(
            attempt.attempt_id, facts.snapshot_reference, "The closing receipt and retained candidate disagree."
        )
    return Completion(attempt.attempt_id, candidate, facts.snapshot_reference)


def select_source(facts: Facts) -> Selection | Unavailable | InvalidEvidence | query_models.DamagedTransitionReceipt:
    attempt = facts.attempt
    if facts.item.state == stored_state.StoredWorkItemState.DONE:
        return _completion_source(facts)
    if attempt is None:
        return Unavailable(facts.item.item_id, facts.item.state, "No current attempt has a reviewed candidate.")
    if attempt.state == work_models.AttemptState.REVIEW and attempt.candidate_revision is not None:
        if facts.snapshot_reference is None:
            return Unavailable(
                facts.item.item_id, facts.item.state, "The retained review candidate has no accepted snapshot bytes."
            )
        return ProtectedReview(attempt.attempt_id, attempt.candidate_revision, facts.snapshot_reference)
    receipt = facts.checkpoint_receipt
    if receipt is None:
        return Unavailable(
            facts.item.item_id,
            facts.item.state,
            "No protected candidate and no checkpoint acceptance exist on the current attempt.",
        )
    try:
        checkpoint = msgspec.json.decode(receipt.outcome_json, type=history.CheckpointAcceptanceOutcome)
    except msgspec.DecodeError as error:
        return query_models.DamagedTransitionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            str(error),
        )
    if facts.package_reference is None:
        return InvalidEvidence(attempt.attempt_id, None, "The checkpoint acceptance has no accepted package reference.")
    return AcceptedCheckpoint(attempt.attempt_id, checkpoint.candidate, checkpoint.checkpoint, facts.package_reference)


def present_source(selected: Selection, compared_from_revision: str) -> Source:
    match selected:
        case ProtectedReview():
            return ProtectedReviewSource(str(selected.attempt_id), selected.candidate, compared_from_revision)
        case AcceptedCheckpoint():
            return AcceptedCheckpointSource(
                str(selected.attempt_id), selected.candidate, compared_from_revision, selected.checkpoint_id
            )
        case Completion():
            return CompletionSource(str(selected.attempt_id), selected.candidate, compared_from_revision)
        case _ as unreachable:
            assert_never(unreachable)
