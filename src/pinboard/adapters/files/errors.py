"""Filesystem-boundary failures for artifact and durable-path operations."""

from pathlib import Path

from pinboard.domain.errors import DescribedCode


class ArtifactErrorCode(DescribedCode):
    STORAGE_INVARIANT_VIOLATION = (
        "STORAGE_INVARIANT_VIOLATION",
        "Stored facts violate a ledger invariant; ordinary transition processing cannot continue.",
    )
    STORAGE_IO_ERROR = ("STORAGE_IO_ERROR", "The ledger read or write encountered a filesystem I/O failure.")


class ArtifactError(RuntimeError):
    code: ArtifactErrorCode

    def __init__(self, code: ArtifactErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


class FileIOErrorCode(DescribedCode):
    DIRECTORY_CREATE_FAILED = (
        "DIRECTORY_CREATE_FAILED",
        "The selected directory could not be created for the requested write.",
    )
    DIRECTORY_INVALID = ("DIRECTORY_INVALID", "The selected directory failed validation for this operation.")
    DIRECTORY_SYNC_FAILED = (
        "DIRECTORY_SYNC_FAILED",
        "The selected directory could not be synced after a filesystem effect.",
    )
    DIRECTORY_VERIFY_FAILED = (
        "DIRECTORY_VERIFY_FAILED",
        "The selected directory could not be verified after creation.",
    )
    FILE_ALREADY_EXISTS = (
        "FILE_ALREADY_EXISTS",
        "An immutable output path already contains bytes, so publication cannot replace it.",
    )
    FILE_PUBLISH_FAILED = (
        "FILE_PUBLISH_FAILED",
        "The selected immutable file could not be published; inspect the returned effect before retrying.",
    )
    VIEW_REFRESH_FAILED = (
        "VIEW_REFRESH_FAILED",
        "Generated view refresh failed, including a failure to open or acquire the board view lock.",
    )


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


class RootErrorCode(DescribedCode):
    PROJECT_GIT_CHECKOUT_UNAVAILABLE = (
        "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
        "The selected project path is not an available Git checkout.",
    )
    PROJECT_GIT_EXCLUDE_UNAVAILABLE = (
        "PROJECT_GIT_EXCLUDE_UNAVAILABLE",
        "The checkout's Git exclusion file could not be read or updated.",
    )
    PROJECT_GIT_LAYOUT_UNSUPPORTED = (
        "PROJECT_GIT_LAYOUT_UNSUPPORTED",
        "The selected Git checkout layout cannot support the requested work-root operation.",
    )
    PROJECT_GIT_ROOT_UNAVAILABLE = (
        "PROJECT_GIT_ROOT_UNAVAILABLE",
        "The selected path could not be resolved to a Git project root.",
    )


class RootError(RuntimeError):
    code: RootErrorCode

    def __init__(self, code: RootErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")
