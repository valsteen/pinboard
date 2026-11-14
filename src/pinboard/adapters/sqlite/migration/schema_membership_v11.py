"""Advance exact v11 ledgers to human-attributed off-roadmap history without changing existing facts."""

import sqlite3
from pathlib import Path

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import database as migration_database


def _upgrade_v11_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 12:
            database._verify_current_schema(connection)
            return False
        database._verify_v11_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v11")
        statements = {name: statement for _, name, _, statement in database._build_expected_schema_signature()}
        for name in ("project_meta", "item_off_roadmap_episodes", "item_active_off_roadmap"):
            statement = statements[name]
            assert statement is not None
            connection.execute(statement)
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 12, revision, host_epoch, created_at, updated_at FROM project_meta_v11"
        )
        connection.execute("DROP TABLE project_meta_v11")
        database._verify_current_schema(connection)
        database.verify_database_integrity(connection)
        if (
            expected_postimage is not None
            and migration_database.schema_logical_digest(connection) != expected_postimage
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Membership migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def migrate_v11_database(path: Path, expected_postimage: str) -> bool:
    with migration_database.migration_connection(path) as connection:
        return _upgrade_v11_connection(connection, expected_postimage)
