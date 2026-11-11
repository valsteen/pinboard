"""Predecessor connections, postimage identity, and the v6 and v7 upgrades."""

import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path

from pinboard.adapters.sqlite import database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode


def _upgrade_v6_connection(connection: sqlite3.Connection) -> bool:
    connection.execute("BEGIN EXCLUSIVE")
    try:
        version = database._read_pinboard_schema_version(connection)
        if version == 7:
            database._verify_v7_schema(connection)
            connection.rollback()
            return False
        if version != 6:
            raise StorageError(StorageErrorCode.SCHEMA_UNSUPPORTED, f"Schema sqlite-v{version} cannot migrate to v7.")
        if database._read_schema_signature(connection) != database._build_v6_schema_signature():
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database does not have the exact v6 schema.")
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v6")
        connection.execute(
            """CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 7),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT"""
        )
        connection.execute(
            """INSERT INTO project_meta (singleton, application, schema_version, revision,
                                         host_epoch, created_at, updated_at)
               SELECT singleton, application, 7, revision, host_epoch, created_at, updated_at
               FROM project_meta_v6"""
        )
        connection.execute("DROP TABLE project_meta_v6")
        connection.execute(
            """CREATE INDEX checkpoint_history_by_subject
ON transition_history(subject_id, history_id)
WHERE outcome_schema = 'checkpoint-acceptance/v2'"""
        )
        database._verify_v7_schema(connection)
        database.verify_database_integrity(connection)
        connection.commit()
        return True
    except StorageError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise


def schema_logical_digest(connection: sqlite3.Connection) -> str:
    """Hash the authoritative schema and rows, independent of SQLite page layout."""

    return sha256("\n".join(connection.iterdump()).encode()).hexdigest()


def inspect_schema_migration(path: Path) -> tuple[int, str, str | None]:
    """Observe exact source bytes and the predicted v7 postimage without writing files."""

    try:
        source_digest = sha256(path.read_bytes()).hexdigest()
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            version = database._read_pinboard_schema_version(connection)
            if version == 7:
                database._verify_v7_schema(connection)
                database.verify_database_integrity(connection)
                return version, source_digest, None
            if version != 6:
                raise StorageError(
                    StorageErrorCode.SCHEMA_UNSUPPORTED, f"Schema sqlite-v{version} cannot migrate to v7."
                )
            if database._read_schema_signature(connection) != database._build_v6_schema_signature():
                raise StorageError(StorageErrorCode.INVALID_STATE, "The database does not have the exact v6 schema.")
            database.verify_database_integrity(connection)
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as simulation:
                connection.backup(simulation)
                database._configure_connection(simulation)
                _upgrade_v6_connection(simulation)
                return version, source_digest, schema_logical_digest(simulation)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The schema migration could not read the database.") from error
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def current_schema_logical_digest(path: Path) -> str:
    """Hash a verified current or retained v7 postimage without changing its plan meaning."""
    try:
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_ONLY), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            version = database._read_pinboard_schema_version(connection)
            if version == 7:
                database._verify_v7_schema(connection)
            elif version == 8:
                database._verify_v8_schema(connection)
            elif version == 9:
                database._verify_v9_schema(connection)
            elif version == 10:
                database._verify_v10_schema(connection)
            else:
                database._verify_current_schema(connection)
            database.verify_database_integrity(connection)
            return schema_logical_digest(connection)
    except sqlite3.Error as error:
        raise database.translate_database_error(error, opening=True).with_database_path(path) from error


def migrate_v6_database(path: Path) -> bool:
    """Upgrade one exact v6 ledger under an exclusive transaction; leave v7 unchanged."""

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            database._build_database_uri(path, OpenMode.READ_WRITE),
            uri=True,
            timeout=database.BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        database._configure_connection(connection)
        connection.execute("PRAGMA synchronous = FULL")
        return _upgrade_v6_connection(connection)
    except StorageError:
        raise
    except sqlite3.Error as error:
        raise database.translate_database_error(error).with_database_path(path) from error
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def migration_connection(path: Path) -> Generator[sqlite3.Connection]:
    """Own a durable predecessor connection while the selected advancement owns its transaction."""
    with closing(
        sqlite3.connect(database._build_database_uri(path, OpenMode.READ_WRITE), uri=True, isolation_level=None)
    ) as connection:
        database._configure_connection(connection)
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        except sqlite3.Error as error:
            raise database.translate_database_error(error).with_database_path(path) from error


def _upgrade_v7_connection(connection: sqlite3.Connection) -> bool:
    """Preserve the released v7-to-v8 step and its retained plan postimage."""
    with database.write_transaction(connection):
        version = database._read_pinboard_schema_version(connection)
        if version == 8:
            database._verify_v8_schema(connection)
            return False
        database._verify_v7_schema(connection)
        database.verify_database_integrity(connection)
        connection.execute("ALTER TABLE project_meta RENAME TO project_meta_v7")
        statement = ""
        for line in (
            Path(__file__)
            .with_name("schema_compatibility_v8.sql")
            .read_text(encoding="utf-8")
            .splitlines(keepends=True)
        ):
            statement += line
            if not sqlite3.complete_statement(statement):
                continue
            sql = "\n".join(part for part in statement.splitlines() if not part.lstrip().startswith("--")).strip()
            if sql.startswith(
                (
                    "CREATE TABLE project_meta",
                    "CREATE TABLE initiative",
                    "CREATE UNIQUE INDEX initiative",
                    "CREATE INDEX initiative",
                )
            ):
                connection.execute(sql)
            statement = ""
        connection.execute(
            "INSERT INTO project_meta SELECT singleton, application, 8, revision, host_epoch, created_at, updated_at FROM project_meta_v7"
        )
        connection.execute("DROP TABLE project_meta_v7")
        database._verify_v8_schema(connection)
        database.verify_database_integrity(connection)
    return True


def migrate_v7_database(path: Path) -> bool:
    try:
        with closing(
            sqlite3.connect(database._build_database_uri(path, OpenMode.READ_WRITE), uri=True, isolation_level=None)
        ) as connection:
            database._configure_connection(connection)
            connection.execute("PRAGMA synchronous = FULL")
            return _upgrade_v7_connection(connection)
    except sqlite3.Error as error:
        raise database.translate_database_error(error).with_database_path(path) from error
