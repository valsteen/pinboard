from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

import msgspec

from pinboard.application import artifacts, stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, ItemId, LeaseId, ProposalId, TaskId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class ProjectStatusFacts:
    project_revision: int
    active_attempts: tuple[AttemptId, ...]
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ProjectOverviewFacts:
    snapshot: LedgerSnapshot
    proposals: tuple[stored_state.StoredProposal, ...]
    preparations: tuple[PreparationAuthorityStatus, ...]


@dataclass(frozen=True, slots=True)
class DecisionScope:
    """Exact persisted relationships whose current facts can affect one decision."""

    item_ids: tuple[ItemId, ...]
    related_item_ids: tuple[ItemId, ...]
    dependency_closure_roots: tuple[ItemId, ...]
    live_dependent_roots: tuple[ItemId, ...]
    attempt_ids: tuple[AttemptId, ...]
    proposal_ids: tuple[ProposalId, ...]
    artifact_ref_ids: tuple[ArtifactRefId, ...]
    completion_history_attempt_ids: tuple[AttemptId, ...]


@dataclass(frozen=True, slots=True)
class AttemptLineage:
    attempt_id: AttemptId
    item_id: ItemId
    branch: str
    base_revision: str


@dataclass(frozen=True, slots=True)
class DecisionFacts:
    snapshot: LedgerSnapshot
    attempt_lineage: tuple[AttemptLineage, ...]


@dataclass(frozen=True, slots=True)
class ItemProjectionFacts:
    item: stored_state.StoredWorkItem
    dependencies: tuple[ItemId, ...]
    overview: OverviewItem | None
    definition: stored_state.ItemDefinitionRevision


@dataclass(frozen=True, slots=True)
class ItemOverviewFacts:
    item: work_models.WorkItem
    dependency_liveness: tuple[tuple[ItemId, bool], ...]
    definition: work_models.DefinitionAnchor
    proposals: tuple[stored_state.StoredProposal, ...]
    preparation: PreparationAuthorityStatus | None
    replacement: work_models.PlannedReplacement | None
    replacement_disposition: work_models.ReplacementDisposition | None


@dataclass(frozen=True, slots=True)
class AttemptProjectionFacts:
    attempt: stored_state.StoredAttempt
    brief_reference: artifacts.BriefArtifactRef | None


@dataclass(frozen=True, slots=True)
class GeneratedViewFacts:
    project_revision: int
    items: tuple[ItemProjectionFacts, ...]
    attempts: tuple[AttemptProjectionFacts, ...]
    receipts: tuple[stored_state.StoredTransitionReceipt, ...]


@dataclass(frozen=True, slots=True)
class AttemptAuthorityStatus:
    attempt_id: AttemptId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    status: authority_models.AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class PreparationAuthorityStatus:
    item_id: ItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    status: authority_models.PreparationLeaseStatus


class ActionContinuation(msgspec.Struct, tag="action", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    action_id: str
    action_kind: decision_models.ActionKind
    condition: str


class ReviewContinuation(
    msgspec.Struct, tag="review-subagent", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: str
    candidate_revision: str
    required_capability: Literal["runtime-subagent"]


class DependencyContinuation(
    msgspec.Struct, tag="wait-for-dependencies", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    dependencies: tuple[str, ...]


class AttemptContinuation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-attempt-continuation/v1"]
    attempt_id: str
    item_id: str
    revision: int
    state: work_models.AttemptState
    owner_task_id: str | None
    terminal: bool
    user_input_required: bool
    next_operation: ActionContinuation | ReviewContinuation | DependencyContinuation | None
    legal_actions: tuple[str, ...]
    forbidden_routes: tuple[Literal["create-user-task", "wake-user-task", "return-ownership-to-parent"], ...]


type NonterminalItemState = Literal[
    stored_state.StoredWorkItemState.ACTIVE,
    stored_state.StoredWorkItemState.PAUSED,
    stored_state.StoredWorkItemState.BLOCKED,
    stored_state.StoredWorkItemState.REVIEW,
]


@dataclass(frozen=True, slots=True)
class AttemptContextItemFacts:
    item_id: ItemId
    subject_revision: str
    state: NonterminalItemState
    current_definition_revision: int
    current_definition_digest: str
    live_dependencies: tuple[ItemId, ...]
    current_replacement_revision: int | None
    replacement_resolved: bool


@dataclass(frozen=True, slots=True)
class TerminalAttemptContextFacts:
    project_revision: int
    attempt_id: AttemptId
    item_id: ItemId


type NonterminalAttemptState = Literal[
    work_models.AttemptState.ACTIVE,
    work_models.AttemptState.PAUSED,
    work_models.AttemptState.BLOCKED,
    work_models.AttemptState.REVIEW,
]


@dataclass(frozen=True, slots=True)
class NonterminalAttemptContextFacts:
    project_revision: int
    attempt_id: AttemptId
    subject_revision: str
    item_id: ItemId
    state: NonterminalAttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    item: AttemptContextItemFacts
    brief_reference: artifacts.BriefArtifactRef


type AttemptContextFacts = TerminalAttemptContextFacts | NonterminalAttemptContextFacts


@dataclass(frozen=True, slots=True)
class ReviewJobContextFacts:
    attempt: AttemptContextFacts
    checkpoint_receipt: stored_state.StoredTransitionReceipt | None
    checkpoint_package_reference: stored_state.ArtifactReference | None
    correction_receipt: stored_state.StoredTransitionReceipt | None


@dataclass(frozen=True, slots=True)
class CompletionCheckpointFacts:
    receipt: stored_state.StoredTransitionReceipt
    package_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class CompletionContextFacts:
    attempt: AttemptContextFacts
    checkpoints: tuple[CompletionCheckpointFacts, ...]


type ItemStatusSchema = Literal["pinboard-item-status/v1"]
type ItemStatusAuthority = Literal["sqlite-v6"]


@dataclass(frozen=True, slots=True)
class ItemStatusItemFacts:
    item_id: ItemId
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    outcome_evidence: str | None
    next_action: str | None
    source: str | None
    notes: str | None
    queue_position: int | None


@dataclass(frozen=True, slots=True)
class ItemStatusAttemptFacts:
    attempt_id: AttemptId
    state: work_models.AttemptState
    candidate_revision: str | None


@dataclass(frozen=True, slots=True)
class ItemStatusLifecycleFacts:
    project_revision: int
    item: ItemStatusItemFacts
    definition_title: str | None
    attempts: tuple[ItemStatusAttemptFacts, ...]


@dataclass(frozen=True, slots=True)
class ItemStatusFacts:
    project_revision: int
    item: ItemStatusItemFacts
    definition_title: str | None
    attempts: tuple[ItemStatusAttemptFacts, ...]
    preparation: PreparationAuthorityStatus | None


class ItemStatusAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    state: work_models.AttemptState
    candidate_revision: str | None


class PreparationStatusView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    definition_revision: int
    definition_digest: str
    task_id: str
    host_id: str
    lease_id: str
    generation: int
    expires_at: str
    status: authority_models.PreparationLeaseStatus


class ItemStatus(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: ItemStatusSchema
    authority: ItemStatusAuthority
    revision: str
    item_id: str
    label: str
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    outcome_evidence: str | None
    next_action: str | None
    source: str | None
    notes: str | None
    queue_position: int | None
    attempts: tuple[ItemStatusAttempt, ...]
    preparation: PreparationStatusView | None


class ParallelSelection(Enum):
    ALL_SAFE = "all-safe"
    SELECTED = "selected"


class ParallelReasonCode(Enum):
    ATTEMPT_OWNED = "attempt-owned"
    DEPENDENCY_LIVE = "dependency-live"
    STATE_NOT_LAUNCHABLE = "state-not-launchable"
    PREPARATION_OWNED = "preparation-owned"


@dataclass(frozen=True, slots=True)
class ParallelSelectionInvalid:
    message: str


@dataclass(frozen=True, slots=True)
class ParallelPreparationFacts:
    status: authority_models.PreparationLeaseStatus
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ParallelAttemptFacts:
    attempt_id: AttemptId
    state: NonterminalAttemptState
    authority_status: authority_models.AttemptLeaseStatus | None
    authority_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ParallelPreviewItemFacts:
    item_id: ItemId
    label: str
    state: work_models.WorkState
    live_dependencies: tuple[ItemId, ...]
    preparation: ParallelPreparationFacts | None
    attempt: ParallelAttemptFacts | None


@dataclass(frozen=True, slots=True)
class ParallelPreviewFacts:
    project_revision: int
    items: tuple[ParallelPreviewItemFacts, ...]


@dataclass(frozen=True, slots=True)
class DependencyReason:
    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewFlag:
    kind: work_models.ProposalRelationKind
    related_item: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class PlannedReplacementWarning:
    relation_revision: int
    replacement_item_id: str
    replacement_cost: str
    temporarily_retained: bool


@dataclass(frozen=True, slots=True)
class OverviewItem:
    item_id: str
    label: str
    effect: str
    unlock: str
    state: work_models.WorkState
    position: int | None
    eligible: bool
    timing: str | None
    depends_on: tuple[str, ...]
    dependency_reasons: tuple[DependencyReason, ...]
    review_flags: tuple[ReviewFlag, ...]
    attempt_id: str | None
    next_action: str | None
    source: str | None
    notes: str | None
    planned_replacement: PlannedReplacementWarning | None
    preparation: PreparationStatusView | None = None


@dataclass(frozen=True, slots=True)
class WorkOverview:
    schema: str
    authority: str
    revision: str
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    immediate_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParallelReason:
    code: ParallelReasonCode
    message: str


@dataclass(frozen=True, slots=True)
class LaunchableParallelItem:
    item_id: str
    label: str
    state: work_models.WorkState
    attempt_id: str | None


@dataclass(frozen=True, slots=True)
class ExcludedParallelItem:
    item_id: str
    label: str
    state: work_models.WorkState
    attempt_id: str | None
    reasons: tuple[ParallelReason, ...]


type ParallelItem = LaunchableParallelItem | ExcludedParallelItem


@dataclass(frozen=True, slots=True)
class ParallelPreview:
    schema: str
    revision: str
    selection: ParallelSelection
    safe: bool
    items: tuple[ParallelItem, ...]


class WorkItemDefinitionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-item-definition/v1"]
    title: str
    objective: str
    hypothesis: str
    evidence: tuple[str, ...]
    scope: tuple[str, ...]
    non_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    effect: str
    unlock: str


class ItemDefinition(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-definition/v1"]
    authority: Literal["sqlite-v6"]
    project_revision: int
    item_id: str
    item_subject_revision: int
    definition_revision: int
    definition_digest: str
    definition: WorkItemDefinitionView


class ItemDefinitionHistoryRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    digest: str
    definition: WorkItemDefinitionView
    reason: str
    source_task: str
    timestamp: str
    before_digest: str | None
    after_digest: str
    committed_project_revision: int


class ItemDefinitionHistory(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-item-definition-history/v1"]
    authority: Literal["sqlite-v6"]
    project_revision: int
    item_id: str
    revisions: tuple[ItemDefinitionHistoryRow, ...]
    next_before_revision: int | None


@dataclass(frozen=True, slots=True)
class ItemDefinitionFacts:
    project_revision: int
    item_subject_revision: int | None
    definition: stored_state.ItemDefinitionRevision | None


@dataclass(frozen=True, slots=True)
class ItemDefinitionHistoryFacts:
    project_revision: int
    item_exists: bool
    revisions: tuple[stored_state.ItemDefinitionRevision, ...]
