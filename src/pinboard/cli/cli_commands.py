from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import msgspec

from pinboard.domain import work_models
from pinboard.domain.identifiers import HostId, TaskId, WorkItemId

_PATH_COMPONENT_ID = msgspec.Meta(min_length=1, pattern=r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z")
_RUNTIME_ID = msgspec.Meta(
    min_length=1,
    pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z",
)
type StableHostId = Annotated[HostId, _RUNTIME_ID]
type StableWorkItemId = Annotated[WorkItemId, _PATH_COMPONENT_ID]
type StableTaskId = Annotated[TaskId, _RUNTIME_ID]


class RootSelection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: Path | None = None
    work_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRoots:
    source_checkout: Path
    shared_repository: Path
    work: Path
    explicit_work_root: bool


class RootCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


class ValidateCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class StatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class DiagnoseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("diagnose output must be JSON")


class CloseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableWorkItemId
    outcome: work_models.CloseOutcome
    reason: str
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ToolContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: str | None = None
    json: bool = False


class CodeCatalogCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool
    code: str | None = None

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("code-catalog output must be JSON")


class ExportCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("export output must be JSON")


class InitializeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class MigrateSchemaPreviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class MigrateSchemaApplyCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    apply: str
    json: bool = False


class MigrateSchemaReverseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    reverse: str
    json: bool = False


class RebuildViewsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


type CliCommand = (
    RootCommand
    | ValidateCommand
    | StatusCommand
    | DiagnoseCommand
    | CloseCommand
    | ToolContractCommand
    | CodeCatalogCommand
    | ExportCommand
    | InitializeCommand
    | MigrateSchemaPreviewCommand
    | MigrateSchemaApplyCommand
    | MigrateSchemaReverseCommand
    | RebuildViewsCommand
)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    roots: RootSelection
    command: CliCommand
