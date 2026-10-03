"""Select one reviewed change for a focused, caller-named content observation."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import (
    candidate_snapshot_compatibility_models,
    candidate_snapshots,
    query_models,
    stored_state,
)
from pinboard.domain import decision_models, history, work_models
from pinboard.domain.identifiers import AttemptId, WorkItemId


class ContentPresence(Enum):
    PRESENT = "content-present"
    NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


type Revision = Annotated[str, msgspec.Meta(pattern=r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\z")]
type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n]+\z")]


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


type Source = ProtectedReview | AcceptedCheckpoint | Completion


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-integration/v1"]
    item_id: NonEmptyLine
    target: NonEmptyLine
    target_revision: Revision
    source: Source
    presence: ContentPresence


@dataclass(frozen=True, slots=True)
class AttemptFacts:
    attempt_id: AttemptId
    item_id: WorkItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None


@dataclass(frozen=True, slots=True)
class ItemFacts:
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    attempt: AttemptFacts | None
    closing_receipt: query_models.ConsumedTransitionReceipt | None
    checkpoint_receipt: query_models.ConsumedTransitionReceipt | None


@dataclass(frozen=True, slots=True)
class ProtectedSelection:
    attempt: AttemptFacts
    candidate: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CompletionSelection:
    attempt: AttemptFacts
    candidate: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    attempt: AttemptFacts
    receipt: stored_state.StoredTransitionReceipt
    outcome: history.CheckpointAcceptanceOutcome


@dataclass(frozen=True, slots=True)
class CandidateUnavailable:
    reason: str


type Selection = ProtectedSelection | CompletionSelection | CheckpointSelection


def select_candidate(facts: ItemFacts) -> Selection | CandidateUnavailable | query_models.DamagedTransitionReceipt:
    """Only current review protection, latest checkpoint acceptance, or completion supplies a source."""
    attempt = facts.attempt
    closing = facts.closing_receipt
    if facts.state == stored_state.StoredWorkItemState.DONE:
        if closing is None or closing.action_kind != decision_models.ActionKind.COMPLETE or attempt is None:
            return CandidateUnavailable("The item was closed directly and has no completion candidate.")
        if attempt.candidate_revision is None or attempt.candidate_recorded_at is None:
            return CandidateUnavailable("The closing attempt has no retained candidate snapshot identity.")
        return CompletionSelection(attempt, attempt.candidate_revision, attempt.candidate_recorded_at)
    if attempt is None:
        return CandidateUnavailable(
            "There is no protected candidate and no checkpoint acceptance on a current attempt."
        )
    if (
        attempt.state == work_models.AttemptState.REVIEW
        and attempt.candidate_revision is not None
        and attempt.candidate_recorded_at is not None
    ):
        return ProtectedSelection(attempt, attempt.candidate_revision, attempt.candidate_recorded_at)
    receipt = facts.checkpoint_receipt
    if receipt is None:
        return CandidateUnavailable(
            "There is no protected candidate and no checkpoint acceptance on the current attempt."
        )
    stored = query_models.stored_consumed_receipt(
        attempt.attempt_id, receipt, decision_models.ActionKind.ACCEPT_CHECKPOINT
    )
    if isinstance(stored, query_models.DamagedTransitionReceipt):
        return stored
    try:
        checkpoint = msgspec.json.decode(bytes(stored.outcome_payload), type=history.CheckpointAcceptanceOutcome)
        if (
            receipt.action_kind != decision_models.ActionKind.ACCEPT_CHECKPOINT
            or checkpoint.outcome != "accept-checkpoint"
            or msgspec.json.encode(checkpoint, order="sorted") != bytes(stored.outcome_payload)
        ):
            raise ValueError("The checkpoint acceptance outcome is not canonical or correlated.")
    except (msgspec.DecodeError, ValueError) as error:
        return query_models.DamagedTransitionReceipt(
            attempt.attempt_id,
            receipt.history_id,
            receipt.committed_at,
            decision_models.ActionKind.ACCEPT_CHECKPOINT,
            str(error),
        )
    return CheckpointSelection(attempt, stored, checkpoint)


@dataclass(frozen=True, slots=True)
class EvidenceInvalid:
    attempt_id: AttemptId
    reference: str
    diagnostic: str


def project_source(
    selection: Selection, snapshot: candidate_snapshots.CandidateSnapshot, reference: stored_state.ArtifactReference
) -> Source | CandidateUnavailable | EvidenceInvalid:
    """Convert the selected snapshot's comparison basis into the source advertised to callers."""
    attempt = selection.attempt
    candidate = snapshot.candidate
    match snapshot:
        case (
            candidate_snapshots.WorkingTreeCandidateSnapshot()
            | candidate_snapshots.DeclaredWorkingTreeCandidateSnapshot()
        ):
            compared_from = snapshot.preimage_revision
        case candidate_snapshots.CommitCandidateSnapshot() | candidate_snapshots.DeclaredCommitCandidateSnapshot():
            compared_from = snapshot.accepted_base_revision
        case candidate_snapshot_compatibility_models.WorkingTreeCandidateSnapshot():
            return CandidateUnavailable("The retained preimage-free candidate cannot supply a compared-from revision.")
        case _ as unreachable:
            assert_never(unreachable)
    match selection:
        case ProtectedSelection():
            if (snapshot.branch, snapshot.accepted_base_revision, snapshot.recorded_at) != (
                attempt.branch,
                attempt.base_revision,
                selection.recorded_at.isoformat(),
            ):
                return EvidenceInvalid(
                    attempt.attempt_id,
                    reference.selector,
                    "The protected attempt does not match its accepted snapshot.",
                )
            source: Source = ProtectedReview(str(attempt.attempt_id), candidate, compared_from)
        case CompletionSelection():
            source = Completion(str(attempt.attempt_id), candidate, compared_from)
        case CheckpointSelection():
            source = AcceptedCheckpoint(str(attempt.attempt_id), candidate, compared_from, selection.outcome.checkpoint)
        case _ as unreachable:
            assert_never(unreachable)
    return source
