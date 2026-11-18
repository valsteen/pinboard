from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Literal

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
    operation_id: ClassVar[Literal["root"]] = "root"


class ValidateCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["validate"]] = "validate"
    json: bool = False


class StatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["status"]] = "status"
    json: bool = False


class DiagnoseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["diagnose"]] = "diagnose"
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("diagnose output must be JSON")


class CloseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["close"]] = "close"
    item_id: StableWorkItemId
    outcome: work_models.DirectCloseOutcome
    reason: str
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ToolContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["tool-contract"]] = "tool-contract"
    operation: str | None = None
    json: bool = False


class CodeCatalogCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["code-catalog"]] = "code-catalog"
    json: bool
    code: str | None = None

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("code-catalog output must be JSON")


class ExportCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["export"]] = "export"
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("export output must be JSON")


class InitializeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["init"]] = "init"
    json: bool = False


class MigrateSchemaPreviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["migrate-schema"]] = "migrate-schema"
    json: bool = False


class MigrateSchemaApplyCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["migrate-schema/apply"]] = "migrate-schema/apply"
    apply: str
    json: bool = False


class MigrateSchemaReverseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["migrate-schema/reverse"]] = "migrate-schema/reverse"
    reverse: str
    json: bool = False


class MigrateSchemaLabelledPreviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Preview the v12 step with one hold label file."""

    operation_id: ClassVar[Literal["migrate-schema/labelled"]] = "migrate-schema/labelled"
    labels: str
    json: bool = False


class MigrateSchemaLabelledApplyCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Apply or resume the v12 step with the exact hold label file its plan was previewed with."""

    operation_id: ClassVar[Literal["migrate-schema/labelled-apply"]] = "migrate-schema/labelled-apply"
    apply: str
    labels: str
    json: bool = False


class RebuildViewsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation_id: ClassVar[Literal["views/rebuild"]] = "views/rebuild"


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
    | MigrateSchemaLabelledPreviewCommand
    | MigrateSchemaLabelledApplyCommand
    | RebuildViewsCommand
)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    roots: RootSelection
    command: CliCommand
