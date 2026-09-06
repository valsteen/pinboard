"""Own SQLite connection setup, schema identity, and publication effects.

Database initialization owns its staging connection and transaction because it
must publish an unverified schema atomically. ``write_transaction`` scopes that
supplied staging or diagnostic connection: it begins, commits, or rolls back,
but never opens or closes the connection and never classifies typed results.
``read_operation`` similarly owns one deferred read transaction so every query
observes one SQLite snapshot, then rolls it back without permitting writes.
Runtime store writes instead own their complete connection and transaction
lifecycle in ``store.py``. This module never obtains time or invokes callbacks.
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from functools import cache
from pathlib import Path
from urllib.parse import quote

import msgspec

from pinboard.adapters.files.errors import FileIOError
from pinboard.adapters.files.file_io import DurableRoots, ensure_directory_chain
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode

APPLICATION = "pinboard"
SCHEMA_VERSION = 3
SCHEMA_ID = "sqlite-v3"
BUSY_TIMEOUT_MS = 2_000


def decode_row[Record](row: sqlite3.Row, record_type: type[Record]) -> Record:
    """Convert one SQLite row into its exact typed boundary record."""

    try:
        return msgspec.convert(dict(row), type=record_type, strict=True)
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Stored row is invalid: {error}") from error


def stale_write(message: str) -> DecisionFailure:
    return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, message)


def require_one_changed_row(cursor: sqlite3.Cursor, message: str) -> DecisionFailure | None:
    """Report a failed compare-and-set without changing transaction ownership."""

    if cursor.rowcount != 1:
        return stale_write(message)
    return None


def _build_database_uri(path: Path, mode: OpenMode, *, immutable: bool = False) -> str:
    immutable_query = "&immutable=1" if immutable else ""
    return f"file:{quote(str(path.absolute()), safe='/')}?mode={mode.value}{immutable_query}"


def translate_database_error(error: sqlite3.Error, *, opening: bool = False) -> StorageError:
    message = str(error).lower()
    if isinstance(error, sqlite3.IntegrityError):
        return StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            "SQLite rejected a relational invariant while persisting a domain decision.",
        )
    if "locked" in message or "busy" in message:
        return StorageError(
            StorageErrorCode.BUSY,
            "SQLite remained busy for the bounded wait; rediscover the action before retrying.",
            retryable=True,
        )
    if any(
        value in message
        for value in ("readonly", "disk i/o", "database or disk is full", "permission", "unable to open database file")
    ):
        return StorageError(StorageErrorCode.IO_ERROR, "SQLite could not durably access the work database.")
    if opening or isinstance(error, sqlite3.DatabaseError):
        return StorageError(StorageErrorCode.INVALID_STATE, "The current SQLite work state is malformed or corrupt.")
    return StorageError(StorageErrorCode.OPERATION_FAILED, "The SQLite storage operation failed.")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or enabled[0] != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "SQLite foreign-key enforcement is unavailable.")


def _configure_writes(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")


def _read_required_metadata(connection: sqlite3.Connection) -> tuple[str, int]:
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_meta'"
        ).fetchone()
        if table is None:
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database has no current project metadata.")
        rows = connection.execute("SELECT application, schema_version FROM project_meta").fetchall()
    except sqlite3.Error as error:
        raise translate_database_error(error, opening=True) from error
    if len(rows) != 1:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database must contain exactly one metadata row.")
    try:
        application, version = msgspec.convert(tuple(rows[0]), type=tuple[str, int], strict=True)
    except msgspec.ValidationError as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, "The database metadata types are invalid.") from error
    return application, version


def _read_schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str | None], ...]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    try:
        return msgspec.convert(
            tuple(tuple(row) for row in rows),
            type=tuple[tuple[str, str, str, str | None], ...],
            strict=True,
        )
    except msgspec.ValidationError as error:
        raise StorageError(
            StorageErrorCode.INVALID_STATE, "The current SQLite schema metadata is malformed."
        ) from error


@cache
def _build_expected_schema_signature() -> tuple[tuple[str, str, str, str | None], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(read_schema_bytes().decode("utf-8"))
        return _read_schema_signature(connection)
    except StorageError:
        raise
    except (sqlite3.Error, UnicodeError) as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The installed SQLite schema is invalid.") from error
    finally:
        connection.close()


def _verify_current_schema(connection: sqlite3.Connection) -> None:
    application, version = _read_required_metadata(connection)
    if application != APPLICATION:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"The database belongs to application {application!r}, not {APPLICATION!r}.",
        )
    if version != SCHEMA_VERSION:
        raise StorageError(
            StorageErrorCode.SCHEMA_UNSUPPORTED,
            f"Schema sqlite-v{version} is not the supported {SCHEMA_ID} schema.",
        )
    try:
        if _read_schema_signature(connection) != _build_expected_schema_signature():
            raise StorageError(StorageErrorCode.INVALID_STATE, "The database does not have the exact current schema.")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
    except sqlite3.Error as error:
        raise translate_database_error(error, opening=True) from error
    if quick_check is None or quick_check[0] != "ok" or foreign_key_errors is not None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "SQLite reported invalid current work state.")


def read_schema_bytes() -> bytes:
    try:
        return Path(__file__).with_name("schema.sql").read_bytes()
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The installed SQLite schema could not be read.") from error


def _sync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        raise StorageError(StorageErrorCode.IO_ERROR, "Directory synchronization is unsupported on this platform.")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_database(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(path.parent)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "The initialized database could not be synchronized.") from error


def _staging_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.pinboard-stage")


def _journal_path(path: Path) -> Path:
    return path.with_name(f"{path.name}-journal")


def _cleanup_database_files(path: Path) -> bool:
    removed = False
    succeeded = True
    for candidate in (_journal_path(path), path):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            succeeded = False
            continue
        try:
            candidate.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            succeeded = False
    if removed:
        try:
            _sync_directory(path.parent)
        except OSError:
            succeeded = False
    return succeeded


def _prepare_database_publication(destination: Path) -> Path:
    staging = _staging_path(destination)
    if destination.exists():
        try:
            resumable = staging.exists() and destination.samefile(staging)
        except OSError as error:
            raise StorageError(
                StorageErrorCode.IO_ERROR, "SQLite publication residue could not be inspected."
            ) from error
        if not resumable:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                f"Database already exists: {destination}",
            )
        if not _cleanup_database_files(destination) or not _cleanup_database_files(staging):
            raise StorageError(StorageErrorCode.IO_ERROR, "SQLite publication residue could not be removed.")
    elif not _cleanup_database_files(staging):
        raise StorageError(StorageErrorCode.IO_ERROR, "SQLite staging residue could not be removed.")
    return staging


def reconcile_database_publication(destination: Path) -> None:
    staging = _staging_path(destination)
    try:
        staging.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "SQLite publication residue could not be inspected.") from error
    try:
        resumable = destination.samefile(staging)
    except OSError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, "SQLite publication residue could not be inspected.") from error
    if not resumable:
        raise StorageError(
            StorageErrorCode.INVARIANT_VIOLATION,
            "SQLite publication residue conflicts with the current database.",
        )
    if not _cleanup_database_files(staging):
        raise StorageError(StorageErrorCode.IO_ERROR, "SQLite publication residue could not be removed.")


def _publish_database(staging: Path, destination: Path) -> None:
    published = False
    try:
        try:
            os.link(staging, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise StorageError(
                StorageErrorCode.INVARIANT_VIOLATION,
                f"Database already exists: {destination}",
            ) from error
        except OSError as error:
            raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite database could not be published.") from error
        published = True
        _sync_database(destination)
        if not _cleanup_database_files(staging):
            raise StorageError(StorageErrorCode.IO_ERROR, "SQLite staging cleanup could not be synchronized.")
    except StorageError:
        if not published or _cleanup_database_files(destination):
            _cleanup_database_files(staging)
        raise


def initialize_database(roots: DurableRoots, now: datetime) -> None:
    try:
        ensure_directory_chain(roots)
    except FileIOError as error:
        raise StorageError(StorageErrorCode.IO_ERROR, str(error)) from error
    path = roots.database_path
    staging = _prepare_database_publication(path)

    connection: sqlite3.Connection | None = None
    try:
        try:
            connection = sqlite3.connect(staging, timeout=BUSY_TIMEOUT_MS / 1_000, isolation_level=None)
            _configure_connection(connection)
            _configure_writes(connection)
            connection.executescript(read_schema_bytes().decode("utf-8"))
            with write_transaction(connection):
                timestamp = now.isoformat()
                connection.execute(
                    """
                    INSERT INTO project_meta (
                        singleton, application, schema_version, revision, host_epoch, created_at, updated_at
                    ) VALUES (1, ?, ?, 0, 1, ?, ?)
                    """,
                    (APPLICATION, SCHEMA_VERSION, timestamp, timestamp),
                )
            _verify_current_schema(connection)
        finally:
            if connection is not None:
                connection.close()
        _sync_database(staging)
    except StorageError:
        _cleanup_database_files(staging)
        raise
    except (OSError, UnicodeError) as error:
        _cleanup_database_files(staging)
        raise StorageError(StorageErrorCode.IO_ERROR, "The SQLite database could not be initialized.") from error
    except sqlite3.Error as error:
        _cleanup_database_files(staging)
        raise translate_database_error(error) from error
    _publish_database(staging, path)


def _open_verified_database(
    path: Path,
    mode: OpenMode,
    *,
    configure_writes: bool,
    immutable: bool = False,
) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _build_database_uri(path, mode, immutable=immutable),
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        _configure_connection(connection)
        _verify_current_schema(connection)
        if configure_writes:
            _configure_writes(connection)
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise translate_database_error(error, opening=True) from error


def open_database(path: Path, mode: OpenMode) -> sqlite3.Connection:
    preflight = _open_verified_database(path, OpenMode.READ_ONLY, configure_writes=False, immutable=True)
    preflight.close()
    return _open_verified_database(path, mode, configure_writes=mode == OpenMode.READ_WRITE)


@contextmanager
def read_operation(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()
    except StorageError:
        raise
    except sqlite3.Error as error:
        raise translate_database_error(error, opening=True) from error


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except sqlite3.Error as error:
            connection.rollback()
            raise translate_database_error(error) from error
        except Exception:
            connection.rollback()
            raise
        try:
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise translate_database_error(error) from error
    except sqlite3.Error as error:
        raise translate_database_error(error) from error
