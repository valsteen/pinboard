from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.identifiers import ActionId, AttemptId, HostId, ItemId, LeaseId, ReviewId, TaskId

_PATH_COMPONENT_ID = msgspec.Meta(min_length=1, pattern=r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z")
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type ItemDefinitionHistoryLimit = Annotated[int, msgspec.Meta(ge=1, le=100)]
type KebabReviewId = Annotated[ReviewId, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type StableActionId = Annotated[ActionId, _PATH_COMPONENT_ID]
type StableAttemptId = Annotated[AttemptId, _PATH_COMPONENT_ID]
type StableHostId = Annotated[HostId, _PATH_COMPONENT_ID]
type StableItemId = Annotated[ItemId, _PATH_COMPONENT_ID]
type StableLeaseId = Annotated[LeaseId, _PATH_COMPONENT_ID]
type StableTaskId = Annotated[TaskId, _PATH_COMPONENT_ID]
type BriefBoundary = Literal["local", "cross-boundary"]
BRIEF_BOUNDARIES: tuple[BriefBoundary, ...] = ("local", "cross-boundary")


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


class OverviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class ItemStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    json: bool = False


class ItemReviseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ItemDefinitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    json: bool = False


class ItemDefinitionHistoryCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    limit: ItemDefinitionHistoryLimit = 20
    before_revision: PositiveInt | None = None
    json: bool = False


class CloseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    outcome: work_models.CloseOutcome
    reason: str
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ActionsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    action_id: StableActionId | None = None
    json: bool = False


class LeasedActionsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: decision_models.Role
    lease_id: StableLeaseId
    generation: int
    action_id: StableActionId | None = None
    json: bool = False


type ActionQueryCommand = ActionsCommand | LeasedActionsCommand


class InputContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_kind: decision_models.ActionKind
    json: bool = False


class ToolContractCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: str | None = None
    action_kind: decision_models.ActionKind | None = None
    brief_starter: BriefBoundary | None = None
    json: bool = False

    def __post_init__(self) -> None:
        if (
            (self.operation is not None and self.action_kind is not None)
            or (self.operation is not None and self.brief_starter is not None)
            or (self.action_kind is not None and self.brief_starter is not None)
        ):
            raise ValueError("--operation, --action-kind, and --brief-starter are mutually exclusive")


class BriefSourcesPlanCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    max_batch_bytes: PositiveInt = 24_000


class BriefSourcesEmitCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    plan: Path
    emit_batch: int


class BriefPublishCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    json: bool = False


class HandoverCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool

    def __post_init__(self) -> None:
        if not self.json:
            raise ValueError("handover output must be JSON")


class InitializeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    json: bool = False


class ProposalCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    file: Path
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class ProjectTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    subject_revision: str
    payload: Path
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class AttemptTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    subject_revision: str
    generation: int
    payload: Path
    lease_id: StableLeaseId
    json: bool = False


class PreparationTransitionCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    subject_revision: str
    generation: int
    payload: Path
    lease_id: StableLeaseId
    json: bool = False


type TransitionCommand = ProjectTransitionCommand | AttemptTransitionCommand | PreparationTransitionCommand


class ProjectDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    subject_revision: str
    task_id: StableTaskId
    host_id: StableHostId
    checkpoint: str
    environment: Path
    prompt: Path | None = None
    json: bool = False


class ProjectReviewedDispatchCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: StableActionId
    subject_revision: str
    task_id: StableTaskId
    host_id: StableHostId
    checkpoint: str
    environment: Path
    brief_review: Path
    review_id: KebabReviewId
    prompt: Path | None = None
    json: bool = False


type DispatchCommand = ProjectDispatchCommand | ProjectReviewedDispatchCommand


class AttemptAcquireCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    json: bool = False


class AttemptRenewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    ttl_seconds: int
    json: bool = False


class AttemptReleaseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    json: bool = False


class AttemptRevokeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    lease_id: StableLeaseId
    generation: int
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class AttemptStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    json: bool = False


class AttemptInspectCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    json: bool = False


class InitialReviewJobCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    candidate_revision: Annotated[str, msgspec.Meta(min_length=1)]
    json: bool = False


class PackageInitialReviewJobCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    candidate_revision: Annotated[str, msgspec.Meta(min_length=1)]
    checkpoint_history_id: PositiveInt
    json: bool = False


class CorrectionReviewJobCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    candidate_revision: Annotated[str, msgspec.Meta(min_length=1)]
    correction_history_id: PositiveInt
    json: bool = False


class PackageCorrectionReviewJobCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: StableAttemptId
    candidate_revision: Annotated[str, msgspec.Meta(min_length=1)]
    checkpoint_history_id: PositiveInt
    correction_history_id: PositiveInt
    json: bool = False


type ReviewJobCommand = (
    InitialReviewJobCommand
    | PackageInitialReviewJobCommand
    | CorrectionReviewJobCommand
    | PackageCorrectionReviewJobCommand
)


type AttemptCommand = (
    AttemptAcquireCommand
    | AttemptRenewCommand
    | AttemptReleaseCommand
    | AttemptRevokeCommand
    | AttemptStatusCommand
    | AttemptInspectCommand
    | ReviewJobCommand
)


class PreparationStartCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: PositiveInt
    json: bool = False


class PreparationAcquireCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    expected_project_revision: str
    expected_item_subject_revision: str
    expected_definition_revision: int
    expected_definition_digest: str
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    json: bool = False


class PreparationTransferCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    task_id: StableTaskId
    host_id: StableHostId
    ttl_seconds: int
    json: bool = False


class PreparationRenewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    lease_id: StableLeaseId
    generation: int
    ttl_seconds: int
    json: bool = False


class PreparationReleaseCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    lease_id: StableLeaseId
    generation: int
    json: bool = False


class PreparationRevokeCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    lease_id: StableLeaseId
    generation: int
    task_id: StableTaskId
    host_id: StableHostId
    json: bool = False


class PreparationStatusCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: StableItemId
    json: bool = False


type PreparationCommand = (
    PreparationStartCommand
    | PreparationAcquireCommand
    | PreparationTransferCommand
    | PreparationRenewCommand
    | PreparationReleaseCommand
    | PreparationRevokeCommand
    | PreparationStatusCommand
)


class ParallelPreviewCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item: list[StableItemId] = []
    json: bool = False

    def __post_init__(self) -> None:
        if len(set(self.item)) != len(self.item):
            raise ValueError("--item values must be unique")


class RebuildViewsCommand(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    pass


type CliCommand = (
    RootCommand
    | ValidateCommand
    | StatusCommand
    | OverviewCommand
    | ItemStatusCommand
    | ItemReviseCommand
    | ItemDefinitionCommand
    | ItemDefinitionHistoryCommand
    | CloseCommand
    | ActionQueryCommand
    | InputContractCommand
    | ToolContractCommand
    | BriefSourcesPlanCommand
    | BriefSourcesEmitCommand
    | BriefPublishCommand
    | HandoverCommand
    | InitializeCommand
    | ProposalCommand
    | TransitionCommand
    | DispatchCommand
    | AttemptCommand
    | PreparationCommand
    | ParallelPreviewCommand
    | RebuildViewsCommand
)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    roots: RootSelection
    command: CliCommand
