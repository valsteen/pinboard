"""Advance v15 to attempt-scoped approvals, preserving effective v15 membership."""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.migration import database as migration_database
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.state import read_state


def upgrade_v15_connection(connection: sqlite3.Connection, expected_postimage: str | None) -> bool:
    """Record formerly inactive approvals and advance authority in one transaction."""

    connection.row_factory = sqlite3.Row
    with database.write_transaction(connection):
        if database._read_pinboard_schema_version(connection) == 16:
            database._verify_current_schema(connection)
            return False
        database._verify_v15_schema(connection)
        database.verify_database_integrity(connection)
        statements = {name: sql for _, name, _, sql in database._build_expected_schema_signature()}
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v15")
        for name in ("project_meta", "attempt_amendment_ends", "review_basis_history_by_subject"):
            statement = statements[name]
            assert statement is not None
            connection.execute(statement)
        connection.execute(
            """INSERT INTO project_meta
               SELECT singleton, application, 16, revision, host_epoch, created_at, updated_at FROM project_meta_v15"""
        )
        connection.execute("DROP TABLE project_meta_v15")
        connection.execute(
            """INSERT INTO attempt_amendment_ends (history_id, superseding_history_id, human_decision)
               SELECT amendment.history_id, NULL, NULL FROM attempt_amendments AS amendment
               JOIN attempts AS attempt ON attempt.attempt_id = amendment.attempt_id
               WHERE amendment.brief_artifact_ref_id IS NOT attempt.brief_artifact_ref_id"""
        )
        database._verify_current_schema(connection)
        database.verify_database_integrity(connection)
        read_state(connection)
        if (
            expected_postimage is not None
            and migration_database.schema_logical_digest(connection) != expected_postimage
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                "Amendment-lifetime migration differs from the previewed postimage; the predecessor remains authoritative.",
            )
    return True


def inspect_v15_migration(path: Path) -> tuple[str, str]:
    """Predict the exact v16 postimage without changing the selected ledger."""
    try:
        source = sha256(path.read_bytes()).hexdigest()
        with (
            closing(sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True)) as ledger,
            closing(sqlite3.connect(":memory:", isolation_level=None)) as simulation,
        ):
            database._configure_connection(ledger)
            ledger.backup(simulation)
            database._configure_connection(simulation)
            upgrade_v15_connection(simulation, None)
            return source, migration_database.schema_logical_digest(simulation)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The v15 migration could not read the database.") from error
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def migrate_v15_database(path: Path, expected_postimage: str) -> bool:
    with migration_database.migration_connection(path) as connection:
        return upgrade_v15_connection(connection, expected_postimage)
