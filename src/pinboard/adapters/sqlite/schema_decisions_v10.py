"""Advance exact v10 ledgers to the indexed latest-decision-batch schema without rewriting receipt history."""

import sqlite3
from pathlib import Path

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode


def _upgrade_v10_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 11:
            database._verify_current_schema(connection)
            return False
        database._verify_v10_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v10")
        for _, name, _, statement in database._build_expected_schema_signature():
            if name in ("project_meta", "attempt_decision_batches"):
                assert statement is not None
                connection.execute(statement)
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 11, revision, host_epoch, created_at, updated_at FROM project_meta_v10"
        )
        connection.execute("DROP TABLE project_meta_v10")
        database._verify_current_schema(connection)
        database.verify_database_integrity(connection)
        if expected_postimage is not None and database.schema_logical_digest(connection) != expected_postimage:
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Decision-batch migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def migrate_v10_database(path: Path, expected_postimage: str) -> bool:
    with database.migration_connection(path) as connection:
        return _upgrade_v10_connection(connection, expected_postimage)
