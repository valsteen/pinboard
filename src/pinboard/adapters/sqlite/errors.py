"""SQLite-boundary failures that preserve transaction and storage diagnostics."""

from enum import Enum

from pinboard.application.ports import WorkStoreError


class StorageErrorCode(Enum):
    BUSY = "STORAGE_BUSY"
    INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    INVALID_STATE = "WORK_STATE_INVALID"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    IO_ERROR = "STORAGE_IO_ERROR"
    OPERATION_FAILED = "STORAGE_OPERATION_FAILED"


class StorageError(WorkStoreError):
    code: StorageErrorCode
    retryable: bool

    def __init__(self, code: StorageErrorCode, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code.value}: {message}")
