"""Keyed item and candidate provenance reads; no review-verdict or retained-attempt walk."""

import sqlite3

import msgspec

from pinboard.adapters.sqlite import lifecycle
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.models import CandidateSnapshotAttemptRow
from pinboard.application import integration, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import WorkItemId


class _ItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    state: stored_state.StoredWorkItemState
    subject_revision: int


def read_item_integration(connection: sqlite3.Connection, item_id: WorkItemId) -> integration.ItemFacts | None:
    row = connection.execute(
        "SELECT item_id, state, subject_revision FROM work_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    item = decode_row(row, _ItemRow)
    closing = None
    checkpoint = None
    attempt = None
    if item.state == stored_state.StoredWorkItemState.DONE:
        closing_row = connection.execute(
            f"SELECT {lifecycle.CONSUMED_RECEIPT_COLUMNS} FROM transition_history WHERE project_revision = ?",
            (item.subject_revision,),
        ).fetchone()
        if closing_row is not None:
            closing = lifecycle.decode_consumed_receipt(closing_row)
        if closing is not None and closing.action_kind == decision_models.ActionKind.COMPLETE:
            attempt_row = connection.execute(
                """SELECT attempt_id, item_id, state, branch, base_revision, candidate_revision, candidate_recorded_at, subject_revision
                   FROM attempts WHERE attempt_id = ?""",
                (closing.subject_id,),
            ).fetchone()
            if attempt_row is not None:
                attempt = decode_row(attempt_row, CandidateSnapshotAttemptRow)
    else:
        attempt_row = connection.execute(
            """SELECT attempt_id, item_id, state, branch, base_revision, candidate_revision, candidate_recorded_at, subject_revision
               FROM attempts INDEXED BY one_live_attempt_per_item WHERE item_id = ? AND state != 'done'""",
            (item_id,),
        ).fetchone()
        if attempt_row is not None:
            attempt = decode_row(attempt_row, CandidateSnapshotAttemptRow)
        if attempt is not None and not (
            attempt.state == work_models.AttemptState.REVIEW and attempt.candidate_revision is not None
        ):
            checkpoint_row = connection.execute(
                f"""SELECT {lifecycle.CONSUMED_RECEIPT_COLUMNS} FROM transition_history
                   WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2'
                   ORDER BY history_id DESC LIMIT 1""",
                (attempt.attempt_id,),
            ).fetchone()
            if checkpoint_row is not None:
                checkpoint = lifecycle.decode_consumed_receipt(checkpoint_row)
    selected_attempt = None
    if attempt is not None:
        selected_attempt = integration.AttemptFacts(
            attempt.attempt_id,
            attempt.item_id,
            attempt.state,
            attempt.branch,
            attempt.base_revision,
            attempt.candidate_revision,
            attempt.candidate_recorded_at,
        )
    return integration.ItemFacts(item_id, item.state, selected_attempt, closing, checkpoint)
