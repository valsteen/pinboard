from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, assert_never

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


class IntegrationRelation(Enum):
    CANDIDATE_INTEGRATED = "candidate-integrated"
    CANDIDATE_PENDING_ON_ACCEPTED_BASE = "candidate-pending-on-accepted-base"
    CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE = "candidate-pending-on-squash-equivalent-base"
    CANDIDATE_RESIDUAL = "candidate-residual"
    DIVERGED = "diverged"
    TARGET_STALE = "target-stale"


class RepositoryPhase(Enum):
    DISPOSITION = "disposition"
    CLEANUP = "cleanup"
    TERMINAL = "terminal"
    CORRECTION = "correction"
    REFRESH = "refresh"


class RuntimeEffect(Enum):
    SOURCE_CHECKOUT = "source-checkout"
    SHARED_WORK_ROOT = "shared-work-root"
    GIT_METADATA = "git-metadata"


class RuntimeEffectStatus(Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"
    NOT_REQUIRED = "not-required"


class CandidateLineage(Enum):
    COMMIT_CURRENT = "commit-current"
    WORKING_TREE_CURRENT = "working-tree-current"
    DRIFTED = "drifted"


class RuntimeEffectObservation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    effect: RuntimeEffect
    status: RuntimeEffectStatus


type RuntimeEffectObservations = Annotated[
    tuple[RuntimeEffectObservation, ...],
    msgspec.Meta(min_length=3, max_length=3),
]


def _validate_reconciliation_phase(relation: IntegrationRelation, phase: RepositoryPhase) -> None:
    match relation:
        case (
            IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE
            | IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE
        ):
            valid = phase == RepositoryPhase.DISPOSITION
        case IntegrationRelation.CANDIDATE_INTEGRATED:
            valid = phase in (RepositoryPhase.CLEANUP, RepositoryPhase.TERMINAL)
        case IntegrationRelation.CANDIDATE_RESIDUAL | IntegrationRelation.DIVERGED:
            valid = phase == RepositoryPhase.CORRECTION
        case IntegrationRelation.TARGET_STALE:
            valid = phase == RepositoryPhase.REFRESH
        case _ as unreachable:
            assert_never(unreachable)
    if not valid:
        raise ValueError("integration relation and repository phase do not describe one legal continuation")


def _required_runtime_effects(phase: RepositoryPhase) -> tuple[bool, bool, bool]:
    match phase:
        case RepositoryPhase.DISPOSITION:
            return True, True, True
        case RepositoryPhase.CLEANUP:
            return True, False, True
        case RepositoryPhase.TERMINAL:
            return False, True, False
        case RepositoryPhase.CORRECTION | RepositoryPhase.REFRESH:
            return False, False, False
        case _ as unreachable:
            assert_never(unreachable)


class AttemptReconciliation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    target_revision: str
    relation: IntegrationRelation
    phase: RepositoryPhase
    effects: RuntimeEffectObservations

    def __post_init__(self) -> None:
        if not self.target_revision:
            raise ValueError("reconciliation target revision must be nonempty")
        if tuple(value.effect for value in self.effects) != (
            RuntimeEffect.SOURCE_CHECKOUT,
            RuntimeEffect.SHARED_WORK_ROOT,
            RuntimeEffect.GIT_METADATA,
        ):
            raise ValueError("runtime effects must be source-checkout, shared-work-root, and git-metadata in order")
        _validate_reconciliation_phase(self.relation, self.phase)
        for observation, needed in zip(self.effects, _required_runtime_effects(self.phase), strict=True):
            if needed == (observation.status == RuntimeEffectStatus.NOT_REQUIRED):
                raise ValueError("runtime effect status does not match whether the repository phase requires it")


class ActionContinuation(msgspec.Struct, tag="action", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    action_id: str
    action_kind: decision_models.ActionKind
    condition: str

    def __post_init__(self) -> None:
        if not self.action_id.startswith(f"{self.action_kind.value}:"):
            raise ValueError("continuation action identity must match its action kind")


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


class RefreshTargetContinuation(
    msgspec.Struct, tag="refresh-target", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str


class PermissionRecoveryContinuation(
    msgspec.Struct, tag="permission-recovery", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str
    effect: RuntimeEffect
    status: RuntimeEffectStatus

    def __post_init__(self) -> None:
        if self.status not in (RuntimeEffectStatus.DENIED, RuntimeEffectStatus.UNKNOWN):
            raise ValueError("permission recovery requires a denied or unknown runtime effect")


class RepositoryDispositionContinuation(
    msgspec.Struct, tag="repository-disposition", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str
    relation: IntegrationRelation

    def __post_init__(self) -> None:
        if self.relation not in (
            IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
            IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
        ):
            raise ValueError("repository disposition requires a pending candidate relation")


class RepositoryCleanupContinuation(
    msgspec.Struct, tag="repository-cleanup", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str


type ReconciliationContinuation = (
    RefreshTargetContinuation
    | PermissionRecoveryContinuation
    | RepositoryDispositionContinuation
    | RepositoryCleanupContinuation
)
type NonterminalContinuationOperation = (
    ActionContinuation | ReviewContinuation | DependencyContinuation | ReconciliationContinuation
)


type NonterminalAttemptState = Literal[
    work_models.AttemptState.ACTIVE,
    work_models.AttemptState.PAUSED,
    work_models.AttemptState.BLOCKED,
    work_models.AttemptState.REVIEW,
]


class AttemptContinuationIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-attempt-continuation/v1"]
    attempt_id: str
    item_id: str
    revision: int


def _continuation_action_subject(
    kind: decision_models.ActionKind,
    attempt_id: str,
    item_id: str,
) -> str:
    match decision_models.action_semantics(kind).subject_kind:
        case decision_models.ActionSubjectKind.ATTEMPT:
            return attempt_id
        case decision_models.ActionSubjectKind.ITEM:
            return item_id
        case decision_models.ActionSubjectKind.LEDGER | decision_models.ActionSubjectKind.PROPOSAL:
            raise ValueError("attempt continuations may contain only attempt and item actions")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_continuation_action(action_id: str, attempt_id: str, item_id: str) -> None:
    kind_value, separator, subject = action_id.partition(":")
    if not separator or not subject:
        raise ValueError("continuation legal action identities must be canonical")
    try:
        kind = decision_models.ActionKind(kind_value)
    except ValueError as error:
        raise ValueError("continuation legal action kind must be known") from error
    if subject != _continuation_action_subject(kind, attempt_id, item_id):
        raise ValueError("continuation legal action must target its parent attempt or item")


class TerminalAttemptContinuation(
    AttemptContinuationIdentity,
    tag="done",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    owner_task_id: None
    terminal: bool
    user_input_required: bool
    next_operation: None
    legal_actions: tuple[()]
    forbidden_routes: tuple[Literal["create-user-task", "wake-user-task", "return-ownership-to-parent"], ...]

    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.DONE

    def __post_init__(self) -> None:
        if not self.attempt_id or not self.item_id or not self.terminal or self.user_input_required:
            raise ValueError("a terminal continuation requires exact terminal flags")
        if self.forbidden_routes != ("create-user-task", "wake-user-task", "return-ownership-to-parent"):
            raise ValueError("an attempt continuation requires the exact forbidden routes")


class NonterminalAttemptContinuationBase(AttemptContinuationIdentity, frozen=True, forbid_unknown_fields=True):
    owner_task_id: str
    terminal: bool
    user_input_required: bool
    next_operation: NonterminalContinuationOperation
    legal_actions: tuple[str, ...]
    forbidden_routes: tuple[Literal["create-user-task", "wake-user-task", "return-ownership-to-parent"], ...]

    def _validate_common(self) -> None:
        if (
            not self.attempt_id
            or not self.item_id
            or self.terminal
            or self.user_input_required
            or not self.owner_task_id
            or not self.legal_actions
        ):
            raise ValueError("a nonterminal continuation requires at least one legal action")
        if self.forbidden_routes != ("create-user-task", "wake-user-task", "return-ownership-to-parent"):
            raise ValueError("an attempt continuation requires the exact forbidden routes")
        if len(set(self.legal_actions)) != len(self.legal_actions):
            raise ValueError("continuation legal actions must be unique")
        for action_id in self.legal_actions:
            _validate_continuation_action(action_id, self.attempt_id, self.item_id)
        operation = self.next_operation
        if isinstance(operation, ActionContinuation):
            if operation.action_id not in self.legal_actions:
                raise ValueError("continuation action must be one of its legal actions")
        elif isinstance(operation, ReviewContinuation):
            if operation.attempt_id != self.attempt_id or not operation.candidate_revision:
                raise ValueError("review continuation must target its parent attempt and candidate")
            review_actions = (
                f"{decision_models.ActionKind.ACCEPT_CHECKPOINT.value}:{self.attempt_id}",
                f"{decision_models.ActionKind.COMPLETE.value}:{self.attempt_id}",
            )
            if not any(action in self.legal_actions for action in review_actions):
                raise ValueError("review continuation requires a matching checkpoint or completion action")
        elif isinstance(operation, DependencyContinuation) and (
            not operation.dependencies or len(set(operation.dependencies)) != len(operation.dependencies)
        ):
            raise ValueError("dependency continuations require unique dependencies")


class ActiveAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="active",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.ACTIVE

    def __post_init__(self) -> None:
        self._validate_common()
        operation = self.next_operation
        if not isinstance(operation, ActionContinuation) or operation.action_kind not in (
            decision_models.ActionKind.CONTINUE,
            decision_models.ActionKind.REBIND_ATTEMPT,
            decision_models.ActionKind.PAUSE,
            decision_models.ActionKind.COMPLETE,
        ):
            raise ValueError("an active continuation requires a continue, rebind, pause, or completion action")


class ReviewAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="review",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.REVIEW

    def __post_init__(self) -> None:
        self._validate_common()
        operation = self.next_operation
        if not (
            isinstance(
                operation,
                (
                    ReviewContinuation,
                    RefreshTargetContinuation,
                    PermissionRecoveryContinuation,
                    RepositoryDispositionContinuation,
                    RepositoryCleanupContinuation,
                ),
            )
            or (
                isinstance(operation, ActionContinuation)
                and operation.action_kind
                in (decision_models.ActionKind.COMPLETE, decision_models.ActionKind.RETURN_FOR_CORRECTION)
            )
        ):
            raise ValueError("a review continuation requires review, completion, or correction work")


class PausedAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="paused",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.PAUSED

    def __post_init__(self) -> None:
        self._validate_common()
        operation = self.next_operation
        if not (
            isinstance(operation, DependencyContinuation)
            or (
                isinstance(operation, ActionContinuation) and operation.action_kind == decision_models.ActionKind.RESUME
            )
        ):
            raise ValueError("a paused continuation requires dependency or resume work")


class BlockedAttemptContinuation(
    NonterminalAttemptContinuationBase,
    tag="blocked",
    tag_field="state",
    frozen=True,
    forbid_unknown_fields=True,
):
    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.BLOCKED

    def __post_init__(self) -> None:
        self._validate_common()
        operation = self.next_operation
        if not (
            isinstance(operation, DependencyContinuation)
            or (
                isinstance(operation, ActionContinuation) and operation.action_kind == decision_models.ActionKind.RESUME
            )
        ):
            raise ValueError("a blocked continuation requires dependency or resume work")


type NonterminalAttemptContinuation = (
    ActiveAttemptContinuation | ReviewAttemptContinuation | PausedAttemptContinuation | BlockedAttemptContinuation
)
type AttemptContinuation = TerminalAttemptContinuation | NonterminalAttemptContinuation


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
    candidate_snapshot: CandidateSnapshotContextFacts | None
    candidate_review_reference: stored_state.ArtifactReference | None
    checkpoint_receipt: stored_state.StoredTransitionReceipt | None
    checkpoint_package_reference: stored_state.ArtifactReference | None
    checkpoint_candidate_reference: stored_state.ArtifactReference | None
    correction_receipt: stored_state.StoredTransitionReceipt | None


@dataclass(frozen=True, slots=True)
class CandidateSnapshotContextFacts:
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None
    receipt: stored_state.StoredTransitionReceipt
    reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class CompletionCheckpointFacts:
    receipt: stored_state.StoredTransitionReceipt
    package_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class CompletionContextFacts:
    attempt: AttemptContextFacts
    checkpoints: tuple[CompletionCheckpointFacts, ...]


@dataclass(frozen=True, slots=True)
class CompletionCandidateRequired:
    """Terminal completion needs the existing protected review candidate."""

    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class CompletionRecoveryRequired:
    attempt_id: AttemptId
    item_id: ItemId
    route: Literal[
        "accept-checkpoint",
        "dispatch",
        "record-replacement",
        "rebind-attempt",
        "retain-temporarily",
        "return-for-correction",
        "submit-review",
    ]
    route_subject: str
    alternative_route: Literal["retain-temporarily"] | None
    alternative_subject: str | None
    reason: str


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


class DependencyReason(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    reason: str


class ReviewFlag(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    kind: work_models.ProposalRelationKind
    related_item: str | None
    reason: str


class PlannedReplacementWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    relation_revision: int
    replacement_item_id: str
    replacement_cost: str
    temporarily_retained: bool


class OverviewItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    preparation: PreparationStatusView | None


class NextUnstarted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    live_dependencies: tuple[str, ...]


class WorkOverview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-overview/v5"]
    authority: Literal["sqlite-v6"]
    revision: str
    active_attempts: tuple[str, ...]
    items: tuple[OverviewItem, ...]
    immediate_options: tuple[str, ...]
    next_unstarted: NextUnstarted | None


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


class ParallelItemView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    outcome: Literal["launchable", "excluded"]
    reasons: tuple[ParallelReason, ...]


class ParallelPreviewView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-parallel-preview/v1"]
    revision: str
    selection: Literal["selected", "all-safe"]
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]


class WorkObligationView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    obligation_id: str
    statement: str
    deferral_policy: work_models.ObligationDeferralPolicy


class WorkItemDefinitionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-item-definition/v2"]
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
    checkout_policy: work_models.CheckoutPolicy
    obligations: tuple[WorkObligationView, ...]


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
