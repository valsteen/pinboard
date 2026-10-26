"""Verified settings and confirmed first-use effects at file boundaries.

Contributor trace settings and per-user host settings share one key-level seeding route: an absent file is
created privately, a missing key is written through Git config on an exclusive lock copy that Git writers and
the pre-runtime launcher also honor, and the stored value is read back before use.
"""

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Literal

from pinboard.adapters.files import git_config
from pinboard.adapters.files.errors import FileIOError, FileIOErrorCode, ImmutableFilePublishedError
from pinboard.adapters.files.file_io import create_immutable


@dataclass(frozen=True)
class SettingEffects:
    parent_creation: Literal["none", "unconfirmed"]
    file_creation: Literal["none", "confirmed", "unconfirmed"]
    key_write: Literal["none", "acknowledged", "unconfirmed"]


class EntryProblem(Enum):
    """Why one stored settings entry was rejected."""

    INVALID_VALUE = auto()
    DUPLICATE_KEY = auto()
    UNKNOWN_KEY = auto()


@dataclass(frozen=True)
class InvalidEntry:
    """One stored settings entry that failed validation, named by its own key rather than the caller's."""

    key: str
    problem: EntryProblem
    diagnostic: str


@dataclass(frozen=True)
class NotRegularFile:
    """The settings path holds a directory, symlink or other non-regular file, so no key can be read there."""

    path: Path


@dataclass(frozen=True)
class NotDirectory:
    """A path where the settings directory belongs exists but is not a directory, so no file can be created."""

    path: Path


@dataclass(frozen=True)
class SeedNotReadBack:
    """A seeded key was written and published but absent when the settings file was read back."""

    key: str


type SettingFailureCause = (
    git_config.ReadFailed
    | git_config.WriteUnconfirmed
    | InvalidEntry
    | NotRegularFile
    | NotDirectory
    | SeedNotReadBack
    | ValueError
    | OSError
    | FileIOError
)


@dataclass(frozen=True)
class SettingResolution[T]:
    path: Path
    value: T
    effects: SettingEffects


class SettingResolutionError(ValueError):
    """A setting could not be verified; effects distinguish confirmed work from uncertainty."""

    def __init__(
        self,
        message: str,
        path: Path,
        effects: SettingEffects,
        cause: SettingFailureCause,
        operation: Literal["read", "create", "stage", "publish"],
        resource: Path,
    ) -> None:
        super().__init__(message)
        self.path: Path = path
        self.effects: SettingEffects = effects
        self.cause: SettingFailureCause = cause
        self.operation: Literal["read", "create", "stage", "publish"] = operation
        self.resource: Path = resource


def observe_or_create_settings_file(path: Path) -> SettingEffects:
    """Observe file presence without masking access errors; preserve this invocation's creation effect."""
    effects = SettingEffects("none", "none", "none")
    try:
        path.stat(follow_symlinks=False)
        missing = False
    except FileNotFoundError:
        missing = True
    except OSError as error:
        raise SettingResolutionError(str(error), path, effects, error, "read", path) from error
    if missing:
        try:
            effects = SettingEffects("none", "confirmed" if create_immutable(path, b"") else "none", "none")
        except ImmutableFilePublishedError as error:
            raise SettingResolutionError(
                str(error), path, SettingEffects("none", "confirmed", "none"), error, "create", path
            ) from error
        except FileIOError as error:
            if error.code != FileIOErrorCode.FILE_ALREADY_EXISTS:
                # Publication wraps the filesystem cause; denied access needs the directory's write repair.
                denied = error.__cause__
                if isinstance(denied, PermissionError):
                    raise SettingResolutionError(f"{error}: {denied}", path, effects, denied, "create", path) from error
                raise SettingResolutionError(str(error), path, effects, error, "create", path) from error
    return effects


def resolve_seeded_key[T](
    path: Path,
    read: Callable[[Path, SettingEffects], T | None],
    key: str,
    seed: str,
    subject: str,
) -> SettingResolution[T]:
    """Read a setting, writing `seed` at `key` only when `read` reports the key missing, then read it back.

    `read` raises SettingResolutionError for unreadable or invalid settings and returns None only when the
    key this caller seeds is absent.
    """
    effects = observe_or_create_settings_file(path)
    operation: Literal["read", "stage", "publish"] = "read"
    resource = path
    try:
        settings = read(path, effects)
        if settings is not None:
            return SettingResolution(path, settings, effects)
        # Recheck while holding the lock; a stale absence cannot replace an explicit choice.
        lock = path.with_name(f"{path.name}.lock")
        operation, resource = "stage", lock
        with lock.open("xb") as staged:
            published = False
            try:
                operation, resource = "read", path
                mode = stat.S_IMODE(path.stat().st_mode)
                operation, resource = "stage", lock
                os.fchmod(staged.fileno(), mode)
                settings = read(path, effects)
                if settings is not None:
                    return SettingResolution(path, settings, effects)
                operation, resource = "read", path
                content = path.read_bytes()
                operation, resource = "stage", lock
                staged.write(content)
                staged.flush()
                written = git_config.add(lock, key, seed)
                if isinstance(written, git_config.WriteUnconfirmed):
                    raise SettingResolutionError(
                        f"Cannot write {subject} {key} in {path}: {written.cause.diagnostic}",
                        path,
                        effects,
                        written,
                        "stage",
                        lock,
                    )
                operation, resource = "publish", path
                lock.replace(path)
                published = True
                effects = SettingEffects(effects.parent_creation, effects.file_creation, "acknowledged")
            finally:
                if not published:
                    lock.unlink(missing_ok=True)
        operation, resource = "read", path
        settings = read(path, effects)
        if settings is None:
            raise SettingResolutionError(
                f"{subject} {key} is missing from {path} when read back after its write.",
                path,
                effects,
                SeedNotReadBack(key),
                "read",
                path,
            )
        return SettingResolution(path, settings, effects)
    except (ValueError, OSError) as error:
        if isinstance(error, SettingResolutionError):
            raise
        raise SettingResolutionError(str(error), path, effects, error, operation, resource) from error
