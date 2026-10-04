"""Keyed item, candidate, closure and latest indexed checkpoint facts."""

import sqlite3

from pinboard.adapters.sqlite import artifacts, state
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.models import HistoryIdRow
from pinboard.application import candidate_snapshots, item_integration, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import WorkItemId


def read_item_integration(
    connection: sqlite3.Connection, item_id: WorkItemId
) -> item_integration.IntegrationFacts | None:
    row = connection.execute(
        "SELECT item_id, state, subject_revision FROM work_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    item = decode_row(row, item_integration.ItemFacts)
    closing = None
    checkpoint = None
    attempt_row = None
    columns = "attempt_id, item_id, state, branch, base_revision, candidate_revision, candidate_recorded_at"
    if item.state == stored_state.StoredWorkItemState.DONE:
        receipt_row = connection.execute(
            "SELECT history_id FROM transition_history WHERE project_revision = ?", (item.subject_revision,)
        ).fetchone()
        if receipt_row is not None:
            closing = state.read_history_receipt(connection, decode_row(receipt_row, HistoryIdRow).history_id)
        if closing is not None and closing.action_kind == decision_models.ActionKind.COMPLETE:
            attempt_row = connection.execute(
                f"SELECT {columns} FROM attempts WHERE attempt_id = ? AND item_id = ?",
                (closing.subject_id, item_id),
            ).fetchone()
    elif stored_state.live_work_state(item.state) is not None:
        attempt_row = connection.execute(
            f"SELECT {columns} FROM attempts INDEXED BY one_live_attempt_per_item WHERE item_id = ? AND state != 'done'",
            (item_id,),
        ).fetchone()
    attempt = None if attempt_row is None else decode_row(attempt_row, item_integration.AttemptFacts)
    reference = None
    if attempt is not None:
        if attempt.candidate_revision is not None and attempt.candidate_recorded_at is not None:
            reference = artifacts.read_latest_artifact_reference(
                connection,
                work_models.ArtifactKind.EVIDENCE,
                candidate_snapshots.candidate_snapshot_artifact_key(
                    str(attempt.attempt_id), attempt.candidate_revision, attempt.candidate_recorded_at.isoformat()
                ),
            )
        elif item.state != stored_state.StoredWorkItemState.DONE:
            receipt_row = connection.execute(
                """SELECT history_id FROM transition_history INDEXED BY checkpoint_history_by_subject
                   WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2'
                   ORDER BY history_id DESC LIMIT 1""",
                (attempt.attempt_id,),
            ).fetchone()
            if receipt_row is not None:
                checkpoint = state.read_history_receipt(connection, decode_row(receipt_row, HistoryIdRow).history_id)
                if checkpoint is not None and checkpoint.artifact_ref_id is not None:
                    reference = artifacts.read_artifact_reference_by_id(connection, checkpoint.artifact_ref_id)
    return item_integration.IntegrationFacts(item, attempt, closing, checkpoint, reference)
