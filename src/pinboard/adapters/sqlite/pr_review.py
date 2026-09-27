"""Exact, append-only SQLite evidence for a human-owned PR review.

The review route uses existing item and history relations. It creates no attempt,
lease, candidate, or Git effect. Each write holds the ordinary SQLite write lock.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.database import decode_row, open_database, read_operation, write_transaction
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import move_item_state_count, read_current_definition
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.application import pr_reviews, stored_state
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import WorkItemId


class _ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: stored_state.StoredWorkItemState
    subject_revision: int


class _HistoryRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    action_kind: decision_models.ActionKind
    input_json: str


class _RevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


class _NextHistoryRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    next_history_id: int


class _PositionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    queue_position: int | None


class _ShiftRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    queue_position: int


class _AttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    has_attempt: int


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    history_id: int
    action_kind: decision_models.ActionKind
    payload: (
        pr_reviews.ReviewBrief
        | pr_reviews.BriefReview
        | pr_reviews.HeadObservation
        | pr_reviews.ReviewRound
        | pr_reviews.ReviewClose
    )


@dataclass(frozen=True, slots=True)
class ReviewState:
    item_id: WorkItemId
    item_state: stored_state.StoredWorkItemState
    has_attempt: bool
    subject_revision: int
    project_revision: int
    definition_revision: int
    definition_digest: str
    evidence: tuple[EvidenceRow, ...]

    @property
    def brief(self) -> tuple[int, pr_reviews.ReviewBrief] | None:
        for row in reversed(self.evidence):
            if isinstance(row.payload, pr_reviews.ReviewBrief):
                return row.history_id, row.payload
        return None

    @property
    def rounds(self) -> tuple[tuple[int, pr_reviews.ReviewRound], ...]:
        return tuple(
            (row.history_id, row.payload) for row in self.evidence if isinstance(row.payload, pr_reviews.ReviewRound)
        )

    @property
    def brief_review(self) -> pr_reviews.BriefReview | None:
        brief = self.brief
        if brief is None:
            return None
        for row in reversed(self.evidence):
            if isinstance(row.payload, pr_reviews.BriefReview) and row.payload.brief_history_id == brief[0]:
                return row.payload
        return None

    @property
    def latest_observation(self) -> pr_reviews.HeadObservation | None:
        for row in reversed(self.evidence):
            if isinstance(row.payload, pr_reviews.HeadObservation):
                return row.payload
        return None

    @property
    def close(self) -> pr_reviews.ReviewClose | None:
        for row in reversed(self.evidence):
            if isinstance(row.payload, pr_reviews.ReviewClose):
                return row.payload
        return None


def _load(connection: sqlite3.Connection, item_id: WorkItemId) -> ReviewState | None:
    raw_item = connection.execute(
        "SELECT state, subject_revision FROM work_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if raw_item is None:
        return None
    item = decode_row(raw_item, _ItemRow)
    definition = read_current_definition(connection, item_id)
    if definition is None:
        raise ValueError("Review item has no accepted definition.")
    raw_project = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if raw_project is None:
        raise ValueError("Project metadata is missing.")
    project = decode_row(raw_project, _RevisionRow)
    attempt = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM attempts WHERE item_id = ?) AS has_attempt", (item_id,)
    ).fetchone()
    if attempt is None:
        raise ValueError("Attempt ownership could not be read.")
    evidence: list[EvidenceRow] = []
    for raw_history in connection.execute(
        """SELECT history_id, action_kind, input_json FROM transition_history
           WHERE subject_id = ? AND action_kind IN
           ('start-pr-review', 'review-pr-brief', 'observe-pr-head', 'record-pr-round', 'close-pr-review')
           ORDER BY history_id""",
        (item_id,),
    ).fetchall():
        history = decode_row(raw_history, _HistoryRow)
        kind = history.action_kind
        raw = history.input_json.encode("utf-8")
        match kind:
            case decision_models.ActionKind.START_PR_REVIEW:
                payload = msgspec.json.decode(raw, type=pr_reviews.ReviewBrief, strict=True)
            case decision_models.ActionKind.REVIEW_PR_BRIEF:
                payload = msgspec.json.decode(raw, type=pr_reviews.BriefReview, strict=True)
            case decision_models.ActionKind.OBSERVE_PR_HEAD:
                payload = msgspec.json.decode(raw, type=pr_reviews.HeadObservation, strict=True)
            case decision_models.ActionKind.RECORD_PR_ROUND:
                payload = msgspec.json.decode(raw, type=pr_reviews.ReviewRound, strict=True)
            case decision_models.ActionKind.CLOSE_PR_REVIEW:
                payload = msgspec.json.decode(raw, type=pr_reviews.ReviewClose, strict=True)
            case _ as unreachable:
                raise ValueError(f"Unsupported PR review history action: {unreachable.value}")
        evidence.append(EvidenceRow(history.history_id, kind, payload))
    return ReviewState(
        item_id,
        item.state,
        decode_row(attempt, _AttemptRow).has_attempt == 1,
        item.subject_revision,
        project.revision,
        definition.revision,
        definition.digest,
        tuple(evidence),
    )


def read(path: Path, item_id: WorkItemId) -> ReviewState | None:
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        with read_operation(connection):
            return _load(connection, item_id)
    finally:
        connection.close()


def validate_review_history(  # noqa: C901, PLR0912, PLR0915
    connection: sqlite3.Connection, item_ids: tuple[WorkItemId, ...] | None
) -> None:
    """Validate the full independent review route during explicit state reads and export."""

    selected = (
        connection.execute(
            """SELECT item_id FROM work_items WHERE state = 'review' AND NOT EXISTS (
             SELECT 1 FROM attempts WHERE attempts.item_id = work_items.item_id AND attempts.state != 'done'
           )
           UNION
           SELECT subject_id AS item_id FROM transition_history WHERE action_kind = 'start-pr-review'"""
        ).fetchall()
        if item_ids is None
        else tuple((item_id,) for item_id in item_ids)
    )
    try:
        for raw_item in selected:
            item_id = WorkItemId(str(raw_item[0]))
            state = _load(connection, item_id)
            if state is None or not state.evidence:
                raise StorageError(StorageErrorCode.INVALID_STATE, "Review-only item has no reviewed brief history.")
            if state.has_attempt:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE, "Review-only item also has an implementation attempt."
                )
            brief_id: int | None = None
            prepared_by: str | None = None
            brief_review: pr_reviews.BriefReview | None = None
            previous: tuple[int, pr_reviews.ReviewRound] | None = None
            observation: pr_reviews.HeadObservation | None = None
            closed = False
            for row in state.evidence:
                if closed or row.payload.item_id != item_id:
                    raise StorageError(
                        StorageErrorCode.INVALID_STATE, "Review evidence is out of order or names another item."
                    )
                match row.payload:
                    case pr_reviews.ReviewBrief():
                        if (
                            connection.execute(
                                """SELECT 1 FROM work_item_definition_revisions WHERE item_id = ?
                               AND definition_revision = ? AND definition_digest = ?""",
                                (item_id, row.payload.definition_revision, row.payload.definition_digest),
                            ).fetchone()
                            is None
                        ):
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE, "Review brief has no accepted definition."
                            )
                        brief_id = row.history_id
                        prepared_by = row.payload.prepared_by_task_id
                        brief_review = None
                    case pr_reviews.BriefReview():
                        if (
                            brief_id is None
                            or row.payload.brief_history_id != brief_id
                            or row.payload.reviewer_task_id == prepared_by
                            or brief_review is not None
                        ):
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE, "Brief review is absent, repeated, or self-reviewed."
                            )
                        brief_review = row.payload
                    case pr_reviews.HeadObservation():
                        if brief_id is None:
                            raise StorageError(StorageErrorCode.INVALID_STATE, "Head observation precedes the brief.")
                        observation = row.payload
                    case pr_reviews.ReviewRound():
                        if (
                            brief_id is None
                            or brief_review is None
                            or brief_review.verdict != "ready"
                            or row.payload.brief_history_id != brief_id
                        ):
                            raise StorageError(StorageErrorCode.INVALID_STATE, "Round has no current reviewed brief.")
                        if row.payload.previous_round_history_id != (None if previous is None else previous[0]):
                            raise StorageError(StorageErrorCode.INVALID_STATE, "Round does not link its predecessor.")
                        if observation is None or (
                            row.payload.observed_head,
                            row.payload.observation_source,
                        ) != (observation.observed_head, observation.observation_source):
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE, "Round skipped the latest observed head or source."
                            )
                        prior = set() if previous is None else {value.finding_id for value in previous[1].findings}
                        if {value.finding_id for value in row.payload.prior_dispositions} != prior:
                            raise StorageError(StorageErrorCode.INVALID_STATE, "Round lost prior finding dispositions.")
                        previous = row.history_id, row.payload
                    case pr_reviews.ReviewClose():
                        if previous is None or (
                            row.payload.final_round_history_id,
                            row.payload.last_reviewed_head,
                        ) != (previous[0], previous[1].observed_head):
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE, "Closure does not name the last reviewed round."
                            )
                        if {value.finding_id for value in row.payload.final_dispositions} != {
                            value.finding_id for value in previous[1].findings
                        }:
                            raise StorageError(
                                StorageErrorCode.INVALID_STATE, "Closure lost final finding dispositions."
                            )
                        pending = (
                            None
                            if observation is None or observation.observed_head == previous[1].observed_head
                            else observation
                        )
                        if (row.payload.newer_observed_head, row.payload.newer_observation_source) != (
                            None if pending is None else pending.observed_head,
                            None if pending is None else pending.observation_source,
                        ):
                            raise StorageError(StorageErrorCode.INVALID_STATE, "Closure hides an unreviewed head.")
                        closed = True
                    case _ as unreachable:
                        assert_never(unreachable)
            if (
                closed
                and state.item_state
                not in {stored_state.StoredWorkItemState.DONE, stored_state.StoredWorkItemState.DROPPED}
            ) or (not closed and state.item_state != stored_state.StoredWorkItemState.REVIEW):
                raise StorageError(StorageErrorCode.INVALID_STATE, "Review item state conflicts with its history.")
    except (msgspec.ValidationError, ValueError) as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored PR review is invalid: {error}") from error


type Payload = (
    pr_reviews.ReviewBrief
    | pr_reviews.BriefReview
    | pr_reviews.HeadObservation
    | pr_reviews.ReviewRound
    | pr_reviews.ReviewClose
)


def _check(state: ReviewState, payload: Payload) -> DecisionFailure | None:  # noqa: C901, PLR0912
    brief = state.brief
    rounds = state.rounds
    last_round = rounds[-1] if rounds else None
    match payload:
        case pr_reviews.ReviewBrief():
            first_start = (
                state.item_state == stored_state.StoredWorkItemState.READY and brief is None and not state.has_attempt
            )
            revised_start = (
                state.item_state == stored_state.StoredWorkItemState.REVIEW
                and brief is not None
                and (
                    (brief[1].definition_revision, brief[1].definition_digest)
                    != (state.definition_revision, state.definition_digest)
                    or (state.brief_review is not None and state.brief_review.verdict == "needs-correction")
                )
            )
            if not (first_start or revised_start):
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "The item is not ready for review.", None
                )
            if (payload.item_id, payload.definition_revision, payload.definition_digest) != (
                state.item_id,
                state.definition_revision,
                state.definition_digest,
            ):
                return DecisionFailure(
                    DecisionFailureCode.ITEM_DEFINITION_STALE,
                    "Review brief does not match the current definition.",
                    None,
                )
        case pr_reviews.BriefReview():
            if state.item_state != stored_state.StoredWorkItemState.REVIEW or brief is None or state.close is not None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "The review brief is not active.", None
                )
            if (
                (payload.item_id, payload.brief_history_id) != (state.item_id, brief[0])
                or payload.reviewer_task_id == brief[1].prepared_by_task_id
                or state.brief_review is not None
            ):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Brief review must be separate and bind the current unreviewed brief.",
                    None,
                )
            if (brief[1].definition_revision, brief[1].definition_digest) != (
                state.definition_revision,
                state.definition_digest,
            ):
                return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_STALE, "The review brief is stale.", None)
        case pr_reviews.HeadObservation():
            if state.item_state != stored_state.StoredWorkItemState.REVIEW or brief is None or state.close is not None:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The review is not active.", None)
            if payload.item_id != state.item_id:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID, "Observation names another item.", None
                )
        case pr_reviews.ReviewRound():
            if (
                state.item_state != stored_state.StoredWorkItemState.REVIEW
                or brief is None
                or state.close is not None
                or state.brief_review is None
                or state.brief_review.verdict != "ready"
            ):
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "The review is not active.", None)
            if (payload.item_id, payload.brief_history_id) != (state.item_id, brief[0]):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID, "Round names another item or brief.", None
                )
            if (brief[1].definition_revision, brief[1].definition_digest) != (
                state.definition_revision,
                state.definition_digest,
            ):
                return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_STALE, "The reviewed brief is stale.", None)
            if payload.previous_round_history_id != (None if last_round is None else last_round[0]):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID, "Round does not link the latest prior round.", None
                )
            observation = state.latest_observation
            if observation is None or (
                payload.observed_head,
                payload.observation_source,
            ) != (observation.observed_head, observation.observation_source):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Round must cite the latest observed head and source.",
                    None,
                )
            if last_round is not None:
                previous = {finding.finding_id for finding in last_round[1].findings}
                supplied = {value.finding_id for value in payload.prior_dispositions}
                if supplied != previous:
                    return DecisionFailure(
                        DecisionFailureCode.TRANSITION_INPUT_INVALID, "Round must dispose of every prior finding.", None
                    )
                carried = {
                    value.finding_id for value in payload.prior_dispositions if value.disposition == "carried-forward"
                }
                if carried != {value.finding_id for value in payload.findings if value.finding_id in previous}:
                    return DecisionFailure(
                        DecisionFailureCode.TRANSITION_INPUT_INVALID,
                        "Carried findings must appear in this round.",
                        None,
                    )
        case pr_reviews.ReviewClose():
            if state.item_state != stored_state.StoredWorkItemState.REVIEW or brief is None or last_round is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "A reviewed round is required for closure.", None
                )
            if (brief[1].definition_revision, brief[1].definition_digest) != (
                state.definition_revision,
                state.definition_digest,
            ) or last_round[1].brief_history_id != brief[0]:
                return DecisionFailure(
                    DecisionFailureCode.ITEM_DEFINITION_STALE,
                    "A current reviewed brief and round are required for closure.",
                    None,
                )
            if (payload.item_id, payload.final_round_history_id, payload.last_reviewed_head) != (
                state.item_id,
                last_round[0],
                last_round[1].observed_head,
            ):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Closure must name the exact last reviewed round.",
                    None,
                )
            if {value.finding_id for value in payload.final_dispositions} != {
                value.finding_id for value in last_round[1].findings
            }:
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID, "Closure must dispose of every final finding.", None
                )
            observation = state.latest_observation
            pending = (
                None if observation is None or observation.observed_head == last_round[1].observed_head else observation
            )
            if (payload.newer_observed_head, payload.newer_observation_source) != (
                None if pending is None else pending.observed_head,
                None if pending is None else pending.observation_source,
            ):
                return DecisionFailure(
                    DecisionFailureCode.TRANSITION_INPUT_INVALID,
                    "Closure must disclose the newer unreviewed head.",
                    None,
                )
        case _ as unreachable:
            assert_never(unreachable)
    return None


def write(  # noqa: C901, PLR0912, PLR0915
    path: Path,
    item_id: WorkItemId,
    expected_subject_revision: int,
    payload: Payload,
    actor_task_id: str,
    actor_host_id: str,
    now: datetime,
) -> DecisionResult[ReviewState]:
    connection = open_database(path, OpenMode.READ_WRITE)
    try:
        with write_transaction(connection):
            state = _load(connection, item_id)
            if state is None:
                return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, "Review item does not exist.", None)
            if state.subject_revision != expected_subject_revision:
                return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Review action is stale.", None)
            if (failure := _check(state, payload)) is not None:
                return failure
            revision = state.project_revision + 1
            raw_history = connection.execute(
                "SELECT COALESCE(MAX(history_id), 0) + 1 AS next_history_id FROM transition_history"
            ).fetchone()
            if raw_history is None:
                raise ValueError("History identity could not be allocated.")
            history_id = decode_row(raw_history, _NextHistoryRow).next_history_id
            match payload:
                case pr_reviews.ReviewBrief():
                    kind = decision_models.ActionKind.START_PR_REVIEW
                    if state.item_state != stored_state.StoredWorkItemState.REVIEW:
                        move_item_state_count(connection, state.item_state, stored_state.StoredWorkItemState.REVIEW)
                    connection.execute(
                        "UPDATE work_items SET state = 'review', subject_revision = ?, updated_at = ? WHERE item_id = ?",
                        (revision, now.isoformat(), item_id),
                    )
                case pr_reviews.BriefReview():
                    kind = decision_models.ActionKind.REVIEW_PR_BRIEF
                    connection.execute(
                        "UPDATE work_items SET subject_revision = ?, updated_at = ? WHERE item_id = ?",
                        (revision, now.isoformat(), item_id),
                    )
                case pr_reviews.HeadObservation():
                    kind = decision_models.ActionKind.OBSERVE_PR_HEAD
                    connection.execute(
                        "UPDATE work_items SET subject_revision = ?, updated_at = ? WHERE item_id = ?",
                        (revision, now.isoformat(), item_id),
                    )
                case pr_reviews.ReviewRound():
                    kind = decision_models.ActionKind.RECORD_PR_ROUND
                    connection.execute(
                        "UPDATE work_items SET subject_revision = ?, updated_at = ? WHERE item_id = ?",
                        (revision, now.isoformat(), item_id),
                    )
                case pr_reviews.ReviewClose():
                    kind = decision_models.ActionKind.CLOSE_PR_REVIEW
                    after = "done" if payload.outcome == "accepted" else "dropped"
                    raw_position = connection.execute(
                        "SELECT queue_position FROM work_items WHERE item_id = ?", (item_id,)
                    ).fetchone()
                    if raw_position is None:
                        raise ValueError("Review item has no live queue position.")
                    position = decode_row(raw_position, _PositionRow).queue_position
                    if position is None:
                        raise ValueError("Review item has no live queue position.")
                    move_item_state_count(connection, state.item_state, stored_state.StoredWorkItemState(after))
                    connection.execute(
                        """UPDATE work_items SET state = ?, outcome_evidence = ?, queue_position = NULL,
                           subject_revision = ?, updated_at = ? WHERE item_id = ?""",
                        (after, payload.human_direction, revision, now.isoformat(), item_id),
                    )
                    for raw_shift in connection.execute(
                        "SELECT item_id, queue_position FROM work_items WHERE queue_position > ? ORDER BY queue_position",
                        (position,),
                    ).fetchall():
                        value = decode_row(raw_shift, _ShiftRow)
                        connection.execute(
                            "UPDATE work_items SET queue_position = ? WHERE item_id = ?",
                            (value.queue_position - 1, value.item_id),
                        )
                case _ as unreachable:
                    assert_never(unreachable)
            canonical = msgspec.json.encode(payload, order="sorted").decode("utf-8")
            connection.execute(
                """INSERT INTO transition_history (
                    history_id, project_revision, action_id, action_kind, subject_id, artifact_ref_id,
                    artifact_kind, authorization_kind, actor_task_id, actor_host_id, input_schema,
                    input_json, outcome_schema, outcome_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 'project', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    history_id,
                    revision,
                    f"{kind.value}:{item_id}",
                    kind.value,
                    item_id,
                    actor_task_id,
                    actor_host_id,
                    payload.schema,
                    canonical,
                    "pinboard-pr-review-effect/v1",
                    msgspec.json.encode(
                        {"item_id": item_id, "history_id": history_id, "revision": revision}, order="sorted"
                    ).decode("utf-8"),
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1",
                (revision, now.isoformat()),
            )
            updated = _load(connection, item_id)
            if updated is None:
                raise ValueError("Committed review item disappeared.")
            return updated
    finally:
        connection.close()
