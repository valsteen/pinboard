"""SQLite-boundary failures that preserve transaction and storage diagnostics."""

from pathlib import Path

from pinboard.application.ports import WorkStoreError
from pinboard.domain.errors import DescribedCode


class StorageErrorCode(DescribedCode):
    BUSY = ("STORAGE_BUSY", "The ledger storage operation could not obtain its execution or storage turn.")
    INVARIANT_VIOLATION = (
        "STORAGE_INVARIANT_VIOLATION",
        "Stored facts violate a ledger invariant; ordinary transition processing cannot continue.",
    )
    INVALID_STATE = ("WORK_STATE_INVALID", "The saved ledger fails structural or relational validation.")
    SCHEMA_UNSUPPORTED = (
        "SCHEMA_UNSUPPORTED",
        "The stored ledger schema uses a schema or layout this installation does not support.",
    )
    IO_ERROR = ("STORAGE_IO_ERROR", "The ledger read or write encountered a filesystem I/O failure.")
    READ_ONLY = (
        "SQLITE_READONLY",
        "The selected SQLite ledger could not be written with current filesystem permissions.",
    )
    OPERATION_FAILED = (
        "STORAGE_OPERATION_FAILED",
        "A ledger operation failed without a more specific classified storage cause.",
    )


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
