from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import artifacts, released_v6_compatibility, stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    LeaseId,
    ProposalId,
    TaskId,
    WorkItemId,
)
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
class LivePortfolioFacts:
    """Live items with direct dependency reasons and open attempts; no authority, receipts, or artifacts."""

    snapshot: LedgerSnapshot
    proposals: tuple[stored_state.StoredProposal, ...]


@dataclass(frozen=True, slots=True)
class DecisionScope:
    """Exact persisted relationships whose current facts can affect one decision."""

    work_item_ids: tuple[WorkItemId, ...]
    related_work_item_ids: tuple[WorkItemId, ...]
    dependency_closure_roots: tuple[WorkItemId, ...]
    live_dependent_roots: tuple[WorkItemId, ...]
    attempt_ids: tuple[AttemptId, ...]
    proposal_ids: tuple[ProposalId, ...]
    artifact_ref_ids: tuple[ArtifactRefId, ...]
    completion_history_attempt_ids: tuple[AttemptId, ...]


@dataclass(frozen=True, slots=True)
class AttemptLineage:
    attempt_id: AttemptId
    work_item_id: WorkItemId
    branch: str
    base_revision: str


@dataclass(frozen=True, slots=True)
class DecisionFacts:
    snapshot: LedgerSnapshot
    attempt_lineage: tuple[AttemptLineage, ...]


@dataclass(frozen=True, slots=True)
class ItemProjectionFacts:
    work_item: stored_state.StoredWorkItem
    dependencies: tuple[WorkItemId, ...]
    overview: OverviewItem | None
    definition: stored_state.ItemDefinitionRevision
    review_history: tuple[stored_state.StoredTransitionReceipt, ...]
    pause_reason: RecordedPauseReason


@dataclass(frozen=True, slots=True)
class ItemOverviewFacts:
    work_item: work_models.WorkItem
    dependency_liveness: tuple[tuple[WorkItemId, bool], ...]
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
    work_item_id: WorkItemId
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


class CommitThenReinspectContinuation(
    msgspec.Struct, tag="commit-then-reinspect", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str
    relation: IntegrationRelation

    def __post_init__(self) -> None:
        if self.relation not in (
            IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
            IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
        ):
            raise ValueError("commit then reinspect requires a pending candidate relation")


class RepositoryCleanupContinuation(
    msgspec.Struct, tag="repository-cleanup", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    target_revision: str


class ReconcileRepositoryContinuation(
    msgspec.Struct, tag="reconcile-repository", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    """A favorably reviewed candidate waits for the caller's repository observations."""

    candidate_revision: str
    condition: str


type ReconciliationContinuation = (
    RefreshTargetContinuation
    | PermissionRecoveryContinuation
    | RepositoryDispositionContinuation
    | CommitThenReinspectContinuation
    | RepositoryCleanupContinuation
)
type NonterminalContinuationOperation = (
    ActionContinuation
    | ReviewContinuation
    | ReconcileRepositoryContinuation
    | DependencyContinuation
    | ReconciliationContinuation
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
        elif isinstance(operation, ReconcileRepositoryContinuation) and (
            not operation.candidate_revision or not operation.condition
        ):
            raise ValueError("repository reconciliation continuation requires its reviewed candidate and condition")
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
                    ReconcileRepositoryContinuation,
                    RefreshTargetContinuation,
                    PermissionRecoveryContinuation,
                    RepositoryDispositionContinuation,
                    CommitThenReinspectContinuation,
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
    pause_reason: str | None
    """The human reason recorded by the current pause; null for a checkpoint pause or an unrecorded reason."""

    @property
    def state(self) -> work_models.AttemptState:
        return work_models.AttemptState.PAUSED

    def __post_init__(self) -> None:
        self._validate_common()
        if self.pause_reason is not None and not self.pause_reason.strip():
            raise ValueError("a recorded pause reason must be nonempty")
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
    work_item_id: WorkItemId
    subject_revision: str
    state: NonterminalItemState
    current_definition_revision: int
    current_definition_digest: str
    live_dependencies: tuple[WorkItemId, ...]
    current_replacement_revision: int | None
    replacement_resolved: bool


@dataclass(frozen=True, slots=True)
class TerminalAttemptContextFacts:
    project_revision: int
    attempt_id: AttemptId
    work_item_id: WorkItemId


@dataclass(frozen=True, slots=True)
class NonterminalAttemptContextFacts:
    project_revision: int
    attempt_id: AttemptId
    subject_revision: str
    work_item_id: WorkItemId
    state: NonterminalAttemptState
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    candidate_revision: str | None
    brief_artifact_ref_id: ArtifactRefId
    work_item: AttemptContextItemFacts
    brief_reference: artifacts.BriefArtifactRef
    pause_reason: str | None


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
    returned_candidate_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class CandidateSnapshotContextFacts:
    attempt_id: AttemptId
    work_item_id: WorkItemId
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
    work_item_id: WorkItemId
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


type ItemStatusSchema = Literal["pinboard-item-status/v2"]
type ItemStatusAuthority = Literal["sqlite-v7"]


type DamagedReceiptActionKind = Literal[
    decision_models.ActionKind.PAUSE,
    decision_models.ActionKind.REBIND_ATTEMPT,
    decision_models.ActionKind.RETURN_FOR_CORRECTION,
    decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
    decision_models.ActionKind.ACCEPT_CHECKPOINT,
]


class DamagedReceiptDiagnosis(Enum):
    """Where a damaged consumed receipt can be diagnosed without repair."""

    VALIDATION = "validation"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class DamagedTransitionReceipt:
    """A consumed receipt whose columns decode but whose current-format outcome or input does not."""

    attempt_id: AttemptId
    history_id: HistoryId
    committed_at: datetime
    action_kind: DamagedReceiptActionKind
    defect: str


type RecordedPauseReason = str | DamagedTransitionReceipt | None


@dataclass(frozen=True, slots=True)
class ConsumedTransitionReceipt:
    """One selected transition-history row with its stored JSON text still undecoded."""

    history_id: HistoryId
    project_revision: int
    action_id: ActionId
    action_kind: decision_models.ActionKind | released_v6_compatibility.HistoricalActionKind
    subject_id: HistorySubjectId
    artifact_ref_id: ArtifactRefId | None
    authorization: decision_models.AuthorizationKind
    actor_task_id: TaskId | None
    actor_host_id: HostId | None
    input_schema: str
    input_json: str
    outcome_schema: str
    outcome_json: str
    committed_at: datetime


type ReviewActionKind = Literal[
    decision_models.ActionKind.SUBMIT_REVIEW,
    decision_models.ActionKind.RETURN_FOR_CORRECTION,
    decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE,
    decision_models.ActionKind.ACCEPT_CHECKPOINT,
]


@dataclass(frozen=True, slots=True)
class ReviewEventFacts:
    """An attempt's latest review-relevant receipt, its review action, and whether a rebind followed it."""

    action_kind: ReviewActionKind
    receipt: ConsumedTransitionReceipt
    rebound_since: bool


@dataclass(frozen=True, slots=True)
class ItemStatusItemFacts:
    work_item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    timing: work_models.Timing | None
    outcome_evidence: str | None
    next_action: str | None
    source: str | None
    notes: str | None
    queue_position: int | None
    subject_revision: int


@dataclass(frozen=True, slots=True)
class ItemStatusAttemptFacts:
    attempt_id: AttemptId
    state: work_models.AttemptState
    branch: str
    candidate_revision: str | None
    pause_reason: RecordedPauseReason
    review_event: ReviewEventFacts | None


@dataclass(frozen=True, slots=True)
class ClosingAttemptFacts:
    attempt_id: AttemptId
    branch: str
    candidate_revision: str | None


@dataclass(frozen=True, slots=True)
class ItemClosureFacts:
    """Row columns of the receipt at a terminal item's subject revision."""

    action_kind: decision_models.ActionKind | released_v6_compatibility.HistoricalActionKind
    committed_at: datetime
    closing_attempt: ClosingAttemptFacts | None


@dataclass(frozen=True, slots=True)
class ItemStatusLifecycleFacts:
    project_revision: int
    work_item: ItemStatusItemFacts
    definition_title: str | None
    attempts: tuple[ItemStatusAttemptFacts, ...]
    closure: ItemClosureFacts | None


@dataclass(frozen=True, slots=True)
class ItemStatusFacts:
    project_revision: int
    work_item: ItemStatusItemFacts
    definition_title: str | None
    attempts: tuple[ItemStatusAttemptFacts, ...]
    closure: ItemClosureFacts | None
    preparation: PreparationAuthorityStatus | None


@dataclass(frozen=True, slots=True)
class ReadyCandidateReview:
    candidate_revision: str
    reference: stored_state.ArtifactReference


class ItemStatusAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    state: work_models.AttemptState
    branch: str
    candidate_revision: str | None
    pause_reason: str | None
    """The reason recorded when the attempt was paused; null unless a recorded pause is current."""


class IntakeContext(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Proposal-time next action and notes recorded at intake; original context, never the current plan."""

    label: Literal["original-context"]
    next_action: str | None
    notes: str | None


class NoReviewVerdict(msgspec.Struct, tag="none", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class ReturnedForCorrectionVerdict(
    msgspec.Struct, tag="returned-for-correction", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    history_id: int
    reason: str | None
    rebound_since_return: bool
    """True when a rebind followed the return: the verdict is then history, not correction authority."""


class CandidateReviewArtifact(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: int
    selector: str
    sha256: str
    size_bytes: int
    accepted_revision: int


class ReadyReviewVerdict(msgspec.Struct, tag="ready", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    candidate_revision: str
    candidate_review: CandidateReviewArtifact


class AcceptedAndContinuedVerdict(
    msgspec.Struct, tag="accepted-and-continued", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    evidence: str | None


class CheckpointAcceptedVerdict(
    msgspec.Struct, tag="checkpoint-accepted", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    checkpoint: str | None


type ReviewVerdict = (
    NoReviewVerdict
    | ReturnedForCorrectionVerdict
    | ReadyReviewVerdict
    | AcceptedAndContinuedVerdict
    | CheckpointAcceptedVerdict
)


class ItemClosureAction(Enum):
    COMPLETE = "complete"
    CLOSE = "close"
    MERGE_PROPOSAL = "merge-proposal"
    CLOSE_PR_REVIEW = "close-pr-review"


class ClosingAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: str
    branch: str
    candidate_revision: str | None


class ItemClosure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """When and by which action a terminal item closed; never a claim that its change was integrated."""

    action: ItemClosureAction
    committed_at: str
    closing_attempt: ClosingAttempt | None


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
    source: str | None
    queue_position: int | None
    intake_context: IntakeContext
    attempts: tuple[ItemStatusAttempt, ...]
    review_verdict: ReviewVerdict
    closure: ItemClosure | None
    preparation: PreparationStatusView | None


@dataclass(frozen=True, slots=True)
class BranchOwnerFacts:
    work_item_id: WorkItemId
    item_state: stored_state.StoredWorkItemState
    attempt_id: AttemptId
    attempt_state: work_models.AttemptState


@dataclass(frozen=True, slots=True)
class BranchOwnersFacts:
    project_revision: int
    owners: tuple[BranchOwnerFacts, ...]


class BranchOwner(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    item_state: stored_state.StoredWorkItemState
    attempt_id: str
    attempt_state: work_models.AttemptState


class BranchOwners(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-branch-owners/v1"]
    authority: ItemStatusAuthority
    revision: str
    branch: str
    owners: Annotated[tuple[BranchOwner, ...], msgspec.Meta(min_length=1)]


@dataclass(frozen=True, slots=True)
class IntegrationCheckpointFacts:
    """The current attempt's latest checkpoint-acceptance receipt and its linked package reference."""

    receipt: ConsumedTransitionReceipt
    package_reference: stored_state.ArtifactReference | None


@dataclass(frozen=True, slots=True)
class IntegrationLiveAttemptFacts:
    attempt_id: AttemptId
    candidate_revision: str | None
    candidate_snapshot: CandidateSnapshotContextFacts | None
    latest_checkpoint: IntegrationCheckpointFacts | None


@dataclass(frozen=True, slots=True)
class IntegrationClosingAttemptFacts:
    attempt_id: AttemptId
    candidate_revision: str | None
    candidate_snapshot: CandidateSnapshotContextFacts | None


@dataclass(frozen=True, slots=True)
class IntegrationFacts:
    """Keyed facts from which the item's reviewed candidate for an integration check is selected."""

    project_revision: int
    work_item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    current_attempt: IntegrationLiveAttemptFacts | None
    closure_action: decision_models.ActionKind | released_v6_compatibility.HistoricalActionKind | None
    closing_attempt: IntegrationClosingAttemptFacts | None


@dataclass(frozen=True, slots=True)
class ProtectedReviewSelection:
    snapshot: CandidateSnapshotContextFacts


@dataclass(frozen=True, slots=True)
class CheckpointSelection:
    attempt_id: AttemptId
    work_item_id: WorkItemId
    checkpoint: str
    candidate: str
    receipt: stored_state.StoredTransitionReceipt
    package_reference: stored_state.ArtifactReference


@dataclass(frozen=True, slots=True)
class CompletionSelection:
    snapshot: CandidateSnapshotContextFacts


type IntegrationSourceSelection = ProtectedReviewSelection | CheckpointSelection | CompletionSelection


class IntegrationUnavailableReason(Enum):
    NO_REVIEWED_CANDIDATE = "no-reviewed-candidate"
    CLOSED_WITHOUT_COMPLETION = "closed-without-completion"
    CHECKPOINT_WITHOUT_CANDIDATE_SNAPSHOT = "checkpoint-without-candidate-snapshot"
    PRE_SNAPSHOT_CANDIDATE = "pre-snapshot-candidate"


@dataclass(frozen=True, slots=True)
class IntegrationCandidateUnavailable:
    """No reviewed candidate with accepted snapshot bytes exists for the item's integration check."""

    work_item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    attempt_id: AttemptId | None
    reason: IntegrationUnavailableReason


class IntegrationPresence(Enum):
    CONTENT_PRESENT = "content-present"
    CONTENT_NOT_PRESENT = "content-not-present"
    NO_CHANGE = "no-change"


class ProtectedReviewIntegrationSource(
    msgspec.Struct, tag="protected-review", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: str
    candidate_revision: str
    compared_from_revision: str


class AcceptedCheckpointIntegrationSource(
    msgspec.Struct, tag="accepted-checkpoint", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: str
    checkpoint: str
    candidate_revision: str
    compared_from_revision: str


class CompletionIntegrationSource(
    msgspec.Struct, tag="completion", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    attempt_id: str
    candidate_revision: str
    compared_from_revision: str


type IntegrationSource = (
    ProtectedReviewIntegrationSource | AcceptedCheckpointIntegrationSource | CompletionIntegrationSource
)


class ItemIntegration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Whether one reviewed candidate's accepted diff is present in a caller-named target's current content.

    content-present means the recorded diff reverse-applies cleanly to the target commit's tree;
    content-not-present never proves that the change was not integrated.
    """

    schema: Literal["pinboard-item-integration/v1"]
    authority: ItemStatusAuthority
    revision: str
    item_id: str
    target: str
    target_revision: str
    source: IntegrationSource
    presence: IntegrationPresence


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
    work_item_id: WorkItemId
    label: str
    state: work_models.WorkState
    live_dependencies: tuple[WorkItemId, ...]
    preparation: ParallelPreparationFacts | None
    attempt: ParallelAttemptFacts | None


@dataclass(frozen=True, slots=True)
class ParallelPreviewFacts:
    project_revision: int
    items: tuple[ParallelPreviewItemFacts, ...]


class DependencyReason(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    reason: str


class ProposalOrigin(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source_task_id: str
    trigger: str
    relation_kind: work_models.ProposalRelationKind
    related_item: str | None
    why_it_matters: str
    disposition: work_models.ProposalDispositionKind | None
    disposition_reason: str | None


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
    proposal_origin: ProposalOrigin | None
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
    schema: Literal["pinboard-overview/v6"]
    authority: Literal["sqlite-v7"]
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
    authority: Literal["sqlite-v7"]
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
    authority: Literal["sqlite-v7"]
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
