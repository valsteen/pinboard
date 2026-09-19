from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import msgspec

from pinboard.domain import work_models
from pinboard.domain.identifiers import HostId, ItemId, TaskId

_PATH_COMPONENT_ID = msgspec.Meta(min_length=1, pattern=r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z")
_RUNTIME_ID = msgspec.Meta(
    min_length=1,
    pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z",
)
type StableHostId = Annotated[HostId, _RUNTIME_ID]
type StableItemId = Annotated[ItemId, _PATH_COMPONENT_ID]
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


class CloseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    outcome: work_models.CloseOutcome
    reason: str
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ToolContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: str | None = None
    json: bool = False


class HandoverCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("handover output must be JSON")


class InitializeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class RebuildViewsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


type CliCommand = (
    RootCommand
    | ValidateCommand
    | StatusCommand
    | CloseCommand
    | ToolContractCommand
    | HandoverCommand
    | InitializeCommand
    | RebuildViewsCommand
)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    roots: RootSelection
    command: CliCommand
