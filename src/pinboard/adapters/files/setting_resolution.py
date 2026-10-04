"""Verified settings and confirmed first-use effects at file boundaries."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pinboard.adapters.files import git_config
from pinboard.adapters.files.errors import FileIOError


@dataclass(frozen=True)
class SettingEffects:
    parent_creation: Literal["none", "unconfirmed"]
    file_creation: Literal["none", "confirmed", "unconfirmed"]
    key_write: Literal["none", "acknowledged", "unconfirmed"]


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
        cause: git_config.ReadFailed | git_config.WriteUnconfirmed | ValueError | OSError | FileIOError,
        operation: Literal["read", "create", "stage", "publish"],
        resource: Path,
    ) -> None:
        super().__init__(message)
        self.path: Path = path
        self.effects: SettingEffects = effects
        self.cause: git_config.ReadFailed | git_config.WriteUnconfirmed | ValueError | OSError | FileIOError = cause
        self.operation = operation
        self.resource = resource
