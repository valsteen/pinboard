"""Write and read attempt amendment rows, the canonical owner of each recorded amendment.

Each row is keyed by the receipt that recorded it and binds the accepted brief the amendment was recorded against,
the submission it cleared, if any, and the review base an amendment-only delta review may compare against.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import amendments, query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId, HistoryId

_COLUMNS = """amendment.history_id, amendment.attempt_id, amendment.prior_attempt_revision,
              amendment.brief_artifact_ref_id, amendment.amendment_class, amendment.change, amendment.reason,
              amendment.human_decision, amendment.cleared_candidate_revision, amendment.cleared_snapshot_ref_id,
              amendment.base_candidate_revision, amendment.base_snapshot_ref_id, amendment.base_review_ref_id,
              history.committed_at AS recorded_at"""
_FROM = """attempt_amendments AS amendment
           JOIN transition_history AS history ON history.history_id = amendment.history_id"""


class _AmendmentRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    attempt_id: AttemptId
    prior_attempt_revision: int
    brief_artifact_ref_id: ArtifactRefId
    amendment_class: work_models.AmendmentClass
    change: str
    reason: str
    human_decision: str
    cleared_candidate_revision: str | None
    cleared_snapshot_ref_id: ArtifactRefId | None
    base_candidate_revision: str | None
    base_snapshot_ref_id: ArtifactRefId | None
    base_review_ref_id: ArtifactRefId | None
    recorded_at: datetime


class _ActionKindRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: decision_models.ActionKind


def _stored(row: sqlite3.Row) -> stored_state.StoredAttemptAmendment:
    value = decode_row(row, _AmendmentRow)
    match value.base_candidate_revision, value.base_snapshot_ref_id, value.base_review_ref_id:
        case str() as candidate, int() as snapshot, int() as review:
            base = work_models.ReviewBase(CandidateId(candidate), ArtifactRefId(snapshot), ArtifactRefId(review))
        case None, None, None:
            base = None
        case _:
            raise StorageError(StorageErrorCode.INVALID_STATE, "A stored amendment review base is incomplete.")
    return stored_state.StoredAttemptAmendment(
        value.history_id,
        value.attempt_id,
        value.prior_attempt_revision,
        value.recorded_at,
        work_models.AttemptAmendment(
            value.amendment_class,
            value.change,
            value.reason,
            value.human_decision,
            value.brief_artifact_ref_id,
            None if value.cleared_candidate_revision is None else CandidateId(value.cleared_candidate_revision),
            value.cleared_snapshot_ref_id,
            base,
        ),
    )


def insert_amendment(
    connection: sqlite3.Connection,
    history_id: HistoryId,
    attempt_id: AttemptId,
    prior_attempt_revision: int,
    amendment: work_models.AttemptAmendment,
) -> None:
    base = amendment.review_base
    connection.execute(
        """
        INSERT INTO attempt_amendments (
            history_id, attempt_id, prior_attempt_revision, brief_artifact_ref_id, brief_artifact_kind,
            amendment_class, change, reason, human_decision, cleared_candidate_revision, cleared_snapshot_ref_id,
            cleared_snapshot_kind, base_candidate_revision, base_snapshot_ref_id, base_snapshot_kind,
            base_review_ref_id, base_review_kind
        ) VALUES (?, ?, ?, ?, 'brief', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            history_id,
            attempt_id,
            prior_attempt_revision,
            amendment.brief_artifact_ref_id,
            amendment.amendment_class.value,
            amendment.change,
            amendment.reason,
            amendment.human_decision,
            None if amendment.cleared_candidate is None else str(amendment.cleared_candidate),
            amendment.cleared_snapshot_artifact_ref_id,
            None if amendment.cleared_snapshot_artifact_ref_id is None else "evidence",
            None if base is None else str(base.candidate),
            None if base is None else base.snapshot_artifact_ref_id,
            None if base is None else "evidence",
            None if base is None else base.ready_review_artifact_ref_id,
            None if base is None else "evidence",
        ),
    )


def read_attempt_amendments(
    connection: sqlite3.Connection, attempt_ids: Iterable[AttemptId]
) -> dict[AttemptId, tuple[stored_state.StoredAttemptAmendment, ...]]:
    """Read the selected attempts' amendments, each attempt's in recording order."""

    selected: dict[AttemptId, list[stored_state.StoredAttemptAmendment]] = {}
    for row in select_by_ids(
        connection,
        f"SELECT {_COLUMNS} FROM {_FROM} WHERE amendment.attempt_id IN ({{ids}}) "
        "ORDER BY amendment.attempt_id, amendment.history_id",
        attempt_ids,
    ):
        amendment = _stored(row)
        selected.setdefault(amendment.attempt_id, []).append(amendment)
    return {attempt_id: tuple(values) for attempt_id, values in selected.items()}


def read_amendments_by_history(
    connection: sqlite3.Connection, history_ids: Iterable[HistoryId]
) -> dict[HistoryId, stored_state.StoredAttemptAmendment]:
    return {
        amendment.history_id: amendment
        for amendment in (
            _stored(row)
            for row in select_by_ids(
                connection, f"SELECT {_COLUMNS} FROM {_FROM} WHERE amendment.history_id IN ({{ids}})", history_ids
            )
        )
    }


def read_all_amendments(connection: sqlite3.Connection) -> tuple[stored_state.StoredAttemptAmendment, ...]:
    return tuple(
        _stored(row)
        for row in connection.execute(f"SELECT {_COLUMNS} FROM {_FROM} ORDER BY amendment.history_id").fetchall()
    )


def read_amendment_at_revision(
    connection: sqlite3.Connection, attempt_id: AttemptId, project_revision: int
) -> stored_state.StoredAttemptAmendment | None:
    """Read the amendment whose receipt committed at one project revision, if that receipt recorded one."""

    row = connection.execute(
        f"SELECT {_COLUMNS} FROM {_FROM} WHERE amendment.attempt_id = ? AND history.project_revision = ?",
        (attempt_id, project_revision),
    ).fetchone()
    return None if row is None else _stored(row)


def read_later_review_kinds(
    connection: sqlite3.Connection, attempt_id: AttemptId, history_id: HistoryId
) -> tuple[decision_models.ActionKind, ...]:
    """Read the review-relevant action kinds this attempt recorded after one receipt, oldest first."""

    return tuple(
        decode_row(row, _ActionKindRow).action_kind
        for row in connection.execute(
            """SELECT action_kind FROM transition_history
               WHERE subject_id = ? AND history_id > ? AND action_kind IN (
                   'submit-review', 'return-for-correction', 'rebind-attempt', 'accept-review-and-continue',
                   'accept-checkpoint')
               ORDER BY history_id""",
            (attempt_id, history_id),
        ).fetchall()
    )


def read_amendment_facts(
    connection: sqlite3.Connection, attempt_id: AttemptId, brief_artifact_ref_id: ArtifactRefId
) -> query_models.AttemptAmendmentFacts:
    recorded = read_attempt_amendments(connection, (attempt_id,)).get(attempt_id, ())
    current = amendments.current_amendments(recorded, brief_artifact_ref_id)
    return query_models.AttemptAmendmentFacts(
        recorded,
        current,
        () if not current else read_later_review_kinds(connection, attempt_id, current[-1].history_id),
    )
