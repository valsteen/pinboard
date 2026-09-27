"""SQLite-boundary failures that preserve transaction and storage diagnostics."""

from enum import Enum
from pathlib import Path

from pinboard.application.ports import WorkStoreError


class StorageErrorCode(Enum):
    BUSY = "STORAGE_BUSY"
    INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    INVALID_STATE = "WORK_STATE_INVALID"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    IO_ERROR = "STORAGE_IO_ERROR"
    READ_ONLY = "SQLITE_READONLY"
    OPERATION_FAILED = "STORAGE_OPERATION_FAILED"


class StorageError(WorkStoreError):
    code: StorageErrorCode
    retryable: bool
    invariant_violation: bool

    def __init__(self, code: StorageErrorCode, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        self.invariant_violation = code in (StorageErrorCode.INVARIANT_VIOLATION, StorageErrorCode.INVALID_STATE)
        super().__init__(f"{code.value}: {message}")

    def with_database_path(self, database_path: Path) -> StorageError:
        if self.code == StorageErrorCode.READ_ONLY:
            return SQLiteReadOnlyError(database_path)
        return self


class SQLiteReadOnlyError(StorageError):
    database_path: Path

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        super().__init__(
            StorageErrorCode.READ_ONLY,
            f"SQLite could not write the Pinboard database at {database_path}; request write access only to "
            f"{database_path.parent} and inspect current Pinboard state before retrying.",
            retryable=False,
        )
