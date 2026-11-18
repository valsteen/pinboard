"""Advance exact v13 ledgers to v14 attempt amendments without changing existing facts.

v14 adds only the attempt_amendments table, so the step creates it empty and advances the version.
"""

import sqlite3
from pathlib import Path

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import database as migration_database


def upgrade_v13_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 14:
            database._verify_v14_schema(connection)
            return False
        database._verify_v13_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v13")
        statements = {name: statement for _, name, _, statement in database._build_v14_schema_signature()}
        for name in ("project_meta", "attempt_amendments", "attempt_amendments_by_attempt"):
            statement = statements[name]
            assert statement is not None
            connection.execute(statement)
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 14, revision, host_epoch, created_at, updated_at FROM project_meta_v13"
        )
        connection.execute("DROP TABLE project_meta_v13")
        database._verify_v14_schema(connection)
        database.verify_database_integrity(connection)
        if (
            expected_postimage is not None
            and migration_database.schema_logical_digest(connection) != expected_postimage
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Amendment migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def migrate_v13_database(path: Path, expected_postimage: str) -> bool:
    with migration_database.migration_connection(path) as connection:
        return upgrade_v13_connection(connection, expected_postimage)
