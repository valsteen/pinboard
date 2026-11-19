"""Write and read attempt amendment rows, the canonical owner of each recorded amendment.

The original row preserves approval and review provenance. An indexed end entry removes its authority without
editing that row or its receipt.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime

import msgspec

from pinboard.adapters.sqlite.database import decode_row, select_by_ids
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.application import decision_batches, query_models, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, CandidateId, HistoryId, WorkItemId

_COLUMNS = """amendment.history_id, amendment.attempt_id, amendment.prior_attempt_revision,
              amendment.brief_artifact_ref_id, amendment.amendment_class, amendment.change, amendment.reason,
              amendment.human_decision, amendment.cleared_candidate_revision, amendment.cleared_snapshot_ref_id,
              amendment.base_candidate_revision, amendment.base_snapshot_ref_id, amendment.base_review_ref_id,
              history.committed_at AS recorded_at, ended.history_id AS end_id,
              ended.superseding_history_id, ended.human_decision AS superseding_human_decision"""
_FROM = """attempt_amendments AS amendment
           JOIN transition_history AS history ON history.history_id = amendment.history_id
           LEFT JOIN attempt_amendment_ends AS ended ON ended.history_id = amendment.history_id"""


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
    end_id: HistoryId | None
    superseding_history_id: HistoryId | None
    superseding_human_decision: str | None


class _ReviewActionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    action_kind: decision_models.ActionKind
    input_schema: str


def _stored(row: sqlite3.Row) -> stored_state.StoredAttemptAmendment:
    """Decode one stored amendment row at the SQLite decoding boundary.

    The review base columns are all set or all null; a partial review base is invalid stored state and raises
    `StorageError`.
    """
    value = decode_row(row, _AmendmentRow)
    match value.base_candidate_revision, value.base_snapshot_ref_id, value.base_review_ref_id:
        case str() as candidate, int() as snapshot, int() as review:
            base = work_models.ReviewBase(CandidateId(candidate), ArtifactRefId(snapshot), ArtifactRefId(review))
        case None, None, None:
            base = None
        case _:
            raise StorageError(StorageErrorCode.INVALID_STATE, "A stored amendment review base is incomplete.")
    match value.end_id, value.superseding_history_id, value.superseding_human_decision:
        case None, None, None:
            end = None
        case int(), None, None:
            end = stored_state.MigratedAmendmentEnd()
        case int(), int() as superseding_history_id, str() as superseding_human_decision:
            end = stored_state.SupersededAmendment(HistoryId(superseding_history_id), superseding_human_decision)
        case _:
            raise StorageError(StorageErrorCode.INVALID_STATE, "A stored amendment end is incomplete.")
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
        end,
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


def insert_supersessions(
    connection: sqlite3.Connection, history_id: HistoryId, supersessions: tuple[work_models.AmendmentSupersession, ...]
) -> None:
    if supersessions:
        connection.executemany(
            "INSERT INTO attempt_amendment_ends (history_id, superseding_history_id, human_decision) VALUES (?, ?, ?)",
            ((value.history_id, history_id, value.human_decision) for value in supersessions),
        )


def read_attempt_amendments(
    connection: sqlite3.Connection, attempt_ids: Iterable[AttemptId], *, current_only: bool
) -> dict[AttemptId, tuple[stored_state.StoredAttemptAmendment, ...]]:
    """Read the selected attempts' amendments, each attempt's in recording order."""

    selected: dict[AttemptId, list[stored_state.StoredAttemptAmendment]] = {}
    membership = "AND ended.history_id IS NULL " if current_only else ""
    for row in select_by_ids(
        connection,
        f"SELECT {_COLUMNS} FROM {_FROM} WHERE amendment.attempt_id IN ({{ids}}) "
        f"{membership}ORDER BY amendment.attempt_id, amendment.history_id",
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


def _read_later_review_actions(
    connection: sqlite3.Connection, attempt_id: AttemptId, item_id: WorkItemId, history_id: HistoryId
) -> tuple[_ReviewActionRow, ...]:
    """Read later review actions for the attempt and possible brief switches for its item, oldest first."""

    return tuple(
        decode_row(row, _ReviewActionRow)
        for row in connection.execute(
            f"""WITH review_subjects(subject_id, is_resume) AS (VALUES (?, 0), (?, 1))
                SELECT history.history_id, history.action_kind, history.input_schema
                FROM review_subjects
                CROSS JOIN transition_history AS history INDEXED BY review_basis_history_by_subject
                WHERE history.subject_id = review_subjects.subject_id
                  AND (history.action_kind = 'resume') = review_subjects.is_resume
                  AND history.history_id > ?
                  AND (
                      history.action_kind IN ('submit-review', 'return-for-correction', 'rebind-attempt',
                                              'accept-review-and-continue', 'accept-checkpoint')
                      OR (history.action_kind = 'resume' AND
                          history.input_schema != '{decision_batches.SAME_BRIEF_RESUME_SCHEMA}')
                  )
                ORDER BY history.history_id""",
            (attempt_id, item_id, history_id),
        ).fetchall()
    )


def read_amendment_facts(
    connection: sqlite3.Connection, attempt_id: AttemptId, item_id: WorkItemId
) -> query_models.AttemptAmendmentFacts:
    current = read_attempt_amendments(connection, (attempt_id,), current_only=True).get(attempt_id, ())
    actions = () if not current else _read_later_review_actions(connection, attempt_id, item_id, current[-1].history_id)
    return query_models.AttemptAmendmentFacts(
        current,
        tuple(action.action_kind for action in actions),
        next(
            (
                action.history_id
                for action in actions
                if action.action_kind == decision_models.ActionKind.RESUME
                and action.input_schema != decision_batches.REVISED_RESUME_SCHEMA
            ),
            None,
        ),
    )
