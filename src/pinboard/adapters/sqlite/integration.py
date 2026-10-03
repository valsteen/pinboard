"""Focused integration reads, with a missing-snapshot completion provenance fallback."""

import sqlite3
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite import lifecycle, state
from pinboard.adapters.sqlite.artifacts import read_latest_artifact_reference
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import CandidateSnapshotAttemptRow, HistoryIdRow
from pinboard.application import candidate_snapshots, integration, stored_state
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
            attempt.subject_revision,
        )
    return integration.ItemFacts(item_id, item.state, selected_attempt, closing, checkpoint)


def read_candidate_reference(
    connection: sqlite3.Connection,
    selection: integration.ProtectedSelection | integration.CompletionSelection,
) -> stored_state.ArtifactReference | None:
    """Return a keyed reference; absence requires canonical pre-snapshot submission provenance.

    StorageError preserves the existing missing-current-reference invariant. Only a
    completion without its reference scans retained submission metadata; ordinary
    inspection does not use this fallback. Decode only the selected submission.
    """
    attempt = selection.attempt
    reference = read_latest_artifact_reference(
        connection,
        work_models.ArtifactKind.EVIDENCE,
        candidate_snapshots.candidate_snapshot_artifact_key(
            str(attempt.attempt_id), selection.candidate, selection.recorded_at.isoformat()
        ),
    )
    if reference is not None:
        return reference
    match selection:
        case integration.ProtectedSelection():
            row = connection.execute(
                "SELECT history_id FROM transition_history WHERE project_revision = ?",
                (attempt.subject_revision,),
            ).fetchone()
        case integration.CompletionSelection():
            row = connection.execute(
                """SELECT history_id FROM transition_history
                   WHERE subject_id = ? AND action_kind = 'submit-review' AND project_revision < ?
                   ORDER BY project_revision DESC LIMIT 1""",
                (attempt.attempt_id, attempt.subject_revision),
            ).fetchone()
        case _ as unreachable:
            assert_never(unreachable)
    receipt = None if row is None else state.read_history_receipt(connection, decode_row(row, HistoryIdRow).history_id)
    try:
        legacy_candidate = None if receipt is None else candidate_snapshots.legacy_review_candidate(receipt)
    except ValueError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, str(error)) from error
    if (
        receipt is not None
        and receipt.subject_id == attempt.attempt_id
        and receipt.artifact_ref_id is None
        and receipt.committed_at == selection.recorded_at
        and legacy_candidate == selection.candidate
    ):
        return None
    raise StorageError(StorageErrorCode.INVALID_STATE, "The protected candidate has no accepted snapshot artifact.")
