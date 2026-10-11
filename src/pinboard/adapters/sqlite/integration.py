"""Keyed integration facts on a supplied read transaction; no artifact or Git I/O."""

import sqlite3

from pinboard.adapters.sqlite.artifacts import read_artifact_reference_by_id, read_latest_artifact_reference
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.lifecycle import CONSUMED_RECEIPT_COLUMNS, decode_consumed_receipt
from pinboard.application import candidate_snapshots, integration, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import WorkItemId


def read_integration_facts(connection: sqlite3.Connection, item_id: WorkItemId) -> integration.Facts | None:
    row = connection.execute(
        "SELECT item_id, state, subject_revision FROM work_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    item = decode_row(row, integration.ItemRow)
    closing = None
    checkpoint = None
    package_reference = None
    snapshot_reference = None
    attempt_columns = "attempt_id, state, candidate_revision, candidate_recorded_at"
    if item.state == stored_state.StoredWorkItemState.DONE:
        closing_row = connection.execute(
            f"SELECT {CONSUMED_RECEIPT_COLUMNS} FROM transition_history WHERE project_revision = ?",
            (item.subject_revision,),
        ).fetchone()
        closing = None if closing_row is None else decode_consumed_receipt(closing_row)
        attempt_row = (
            connection.execute(
                f"SELECT {attempt_columns} FROM attempts WHERE attempt_id = ?", (closing.subject_id,)
            ).fetchone()
            if closing is not None and closing.action_kind == decision_models.ActionKind.COMPLETE
            else None
        )
    else:
        attempt_row = connection.execute(
            f"SELECT {attempt_columns} FROM attempts INDEXED BY one_live_attempt_per_item WHERE item_id = ? AND state != 'done'",
            (item_id,),
        ).fetchone()
    attempt = None if attempt_row is None else decode_row(attempt_row, integration.AttemptRow)
    if attempt is not None:
        if attempt.candidate_revision is not None and attempt.candidate_recorded_at is not None:
            key = candidate_snapshots.candidate_snapshot_artifact_key(
                str(attempt.attempt_id), attempt.candidate_revision, attempt.candidate_recorded_at.isoformat()
            )
            snapshot_reference = read_latest_artifact_reference(connection, work_models.ArtifactKind.EVIDENCE, key)
        elif item.state != stored_state.StoredWorkItemState.DONE:
            checkpoint_row = connection.execute(
                f"SELECT {CONSUMED_RECEIPT_COLUMNS} FROM transition_history WHERE subject_id = ? AND outcome_schema = 'checkpoint-acceptance/v2' ORDER BY history_id DESC LIMIT 1",
                (attempt.attempt_id,),
            ).fetchone()
            if checkpoint_row is not None:
                checkpoint = decode_consumed_receipt(checkpoint_row)
                if checkpoint.artifact_ref_id is not None:
                    package_reference = read_artifact_reference_by_id(connection, checkpoint.artifact_ref_id)
    return integration.Facts(item, attempt, closing, checkpoint, snapshot_reference, package_reference)
