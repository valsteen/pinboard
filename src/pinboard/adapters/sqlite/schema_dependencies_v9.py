"""Advance exact v9 ledgers without rewriting definitions, receipts, or accepted artifacts."""

import sqlite3
from pathlib import Path

import msgspec

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.domain.history import CheckpointAcceptanceOutcome
from pinboard.domain.identifiers import HistoryId, WorkItemId


class _CheckpointReceipt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId
    action_kind: str
    item_id: WorkItemId | None
    subject_id: str
    outcome_json: str


def _backfill_checkpoint_acceptances(connection: sqlite3.Connection) -> None:
    for row in connection.execute(
        """SELECT receipt.history_id, receipt.action_kind, attempt.item_id, receipt.subject_id, receipt.outcome_json
           FROM transition_history AS receipt
           LEFT JOIN attempts AS attempt ON attempt.attempt_id = receipt.subject_id
           WHERE receipt.outcome_schema = 'checkpoint-acceptance/v2'
           ORDER BY receipt.history_id"""
    ).fetchall():
        receipt = database.decode_row(row, _CheckpointReceipt)
        if receipt.action_kind != "accept-checkpoint":
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Checkpoint receipt '{receipt.history_id}' does not record an accept-checkpoint action.",
            )
        if receipt.item_id is None:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Checkpoint receipt '{receipt.history_id}' has no owning attempt '{receipt.subject_id}'.",
            )
        try:
            outcome = msgspec.json.decode(receipt.outcome_json, type=CheckpointAcceptanceOutcome, strict=True)
        except msgspec.DecodeError as error:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Checkpoint receipt '{receipt.history_id}' has invalid acceptance facts: {error}",
            ) from error
        if msgspec.json.encode(outcome, order="sorted") != receipt.outcome_json.encode("utf-8"):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Checkpoint receipt '{receipt.history_id}' does not use canonical acceptance bytes.",
            )
        connection.execute(
            "INSERT INTO checkpoint_acceptances (history_id, item_id, checkpoint_id) VALUES (?, ?, ?)",
            (receipt.history_id, receipt.item_id, outcome.checkpoint),
        )


def _upgrade_v9_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    """Backfill receipt facts and switch authority atomically, rolling back any failed validation."""
    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 10:
            database._verify_v10_schema(connection)
            return False
        database._verify_v9_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v9")
        for _, name, _, statement in sorted(
            database._build_v10_schema_signature(), key=lambda value: value[0] != "table"
        ):
            if name in ("project_meta", "checkpoint_acceptances", "checkpoint_acceptances_by_dependency"):
                assert statement is not None
                connection.execute(statement)
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 10, revision, host_epoch, created_at, updated_at FROM project_meta_v9"
        )
        connection.execute("DROP TABLE project_meta_v9")
        connection.execute(
            "ALTER TABLE item_dependencies ADD COLUMN checkpoint_id TEXT CHECK (checkpoint_id IS NULL OR length(checkpoint_id) >= 1)"
        )
        connection.execute(
            "ALTER TABLE proposals ADD COLUMN relation_checkpoint_id TEXT CHECK (relation_checkpoint_id IS NULL OR (relation_kind = 'follow-up' AND length(relation_checkpoint_id) >= 1))"
        )
        _backfill_checkpoint_acceptances(connection)
        database._verify_v10_schema(connection)
        database.verify_database_integrity(connection)
        if expected_postimage is not None and database.schema_logical_digest(connection) != expected_postimage:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Dependency migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def migrate_v9_database(path: Path, expected_postimage: str) -> bool:
    with database.migration_connection(path) as connection:
        return _upgrade_v9_connection(connection, expected_postimage)
