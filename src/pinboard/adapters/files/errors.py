"""Filesystem-boundary failures for artifact and durable-path operations."""

from enum import Enum
from pathlib import Path


class ArtifactErrorCode(Enum):
    STORAGE_INVARIANT_VIOLATION = "STORAGE_INVARIANT_VIOLATION"
    STORAGE_IO_ERROR = "STORAGE_IO_ERROR"


class ArtifactError(RuntimeError):
    code: ArtifactErrorCode

    def __init__(self, code: ArtifactErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class FileIOErrorCode(Enum):
    DIRECTORY_CREATE_FAILED = "DIRECTORY_CREATE_FAILED"
    DIRECTORY_INVALID = "DIRECTORY_INVALID"
    DIRECTORY_SYNC_FAILED = "DIRECTORY_SYNC_FAILED"
    DIRECTORY_VERIFY_FAILED = "DIRECTORY_VERIFY_FAILED"
    FILE_ALREADY_EXISTS = "FILE_ALREADY_EXISTS"
    FILE_PUBLISH_FAILED = "FILE_PUBLISH_FAILED"
    VIEW_REFRESH_FAILED = "VIEW_REFRESH_FAILED"


class FileIOError(RuntimeError):
    code: FileIOErrorCode

    def __init__(self, code: FileIOErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class ImmutableFilePublishedError(FileIOError):
    """Directory synchronization failed after an immutable destination became visible."""

    path: Path

    def __init__(self, path: Path, cause: FileIOError) -> None:
        self.path = path
        super().__init__(cause.code, f"Immutable file was published before synchronization failed: {path}")


class RootErrorCode(Enum):
    PROJECT_GIT_EXCLUDE_UNAVAILABLE = "PROJECT_GIT_EXCLUDE_UNAVAILABLE"
    PROJECT_GIT_LAYOUT_UNSUPPORTED = "PROJECT_GIT_LAYOUT_UNSUPPORTED"
    PROJECT_GIT_ROOT_UNAVAILABLE = "PROJECT_GIT_ROOT_UNAVAILABLE"


class RootError(RuntimeError):
    code: RootErrorCode

    def __init__(self, code: RootErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")
