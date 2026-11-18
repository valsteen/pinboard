from collections.abc import Collection, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, NewType, assert_never

from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    DecisionId,
    HistoryId,
    HostId,
    LeaseId,
    ProposalId,
    TaskId,
    WorkItemId,
)

ObligationId = NewType("ObligationId", str)

CanonicalJson = NewType("CanonicalJson", bytes)


@dataclass(frozen=True, slots=True)
class CheckpointDependency:
    item_id: WorkItemId
    checkpoint_id: CheckpointId


type Dependency = WorkItemId | CheckpointDependency


def dependency_item(value: Dependency) -> WorkItemId:
    match value:
        case str():
            return value
        case CheckpointDependency(item_id=item_id):
            return item_id
        case _ as unreachable:
            assert_never(unreachable)


class PrerequisiteOutcome(Enum):
    """Where a prerequisite item stands in the snapshot: still live, finished (done or superseded) or cancelled."""

    LIVE = "live"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class DependencyFate(Enum):
    """What one accepted dependency is now; the one owner of dependency satisfaction.

    An accepted checkpoint stays accepted whatever later happens to its item, so it takes precedence. A cancelled
    prerequisite will never finish, so the dependency can never be satisfied until the dependent's definition changes.
    """

    AWAITING_ITEM = "awaiting-item"
    AWAITING_CHECKPOINT = "awaiting-checkpoint"
    ITEM_FINISHED = "item-finished"
    CHECKPOINT_ACCEPTED = "checkpoint-accepted"
    ITEM_CANCELLED = "item-cancelled"

    @property
    def satisfied(self) -> bool:
        match self:
            case DependencyFate.ITEM_FINISHED | DependencyFate.CHECKPOINT_ACCEPTED:
                return True
            case DependencyFate.AWAITING_ITEM | DependencyFate.AWAITING_CHECKPOINT | DependencyFate.ITEM_CANCELLED:
                return False
            case _ as unreachable:
                assert_never(unreachable)


def dependency_fate(
    value: Dependency, prerequisite: PrerequisiteOutcome, accepted_checkpoints: Set[CheckpointDependency]
) -> DependencyFate:
    if isinstance(value, CheckpointDependency) and value in accepted_checkpoints:
        return DependencyFate.CHECKPOINT_ACCEPTED
    match prerequisite:
        case PrerequisiteOutcome.FINISHED:
            return DependencyFate.ITEM_FINISHED
        case PrerequisiteOutcome.CANCELLED:
            return DependencyFate.ITEM_CANCELLED
        case PrerequisiteOutcome.LIVE:
            return DependencyFate.AWAITING_ITEM if isinstance(value, str) else DependencyFate.AWAITING_CHECKPOINT
        case _ as unreachable:
            assert_never(unreachable)


def dependency_satisfied(
    value: Dependency, prerequisite_satisfied: bool, accepted_checkpoints: Set[CheckpointDependency]
) -> bool:
    """Whether a dependency is satisfied, for callers that know only whether its prerequisite finished.

    A live and a cancelled prerequisite are equally unsatisfied, so this derives satisfaction from the fate without
    distinguishing them; a caller that describes the dependency uses ``dependency_fate`` with the actual outcome.
    """

    return dependency_fate(
        value,
        PrerequisiteOutcome.FINISHED if prerequisite_satisfied else PrerequisiteOutcome.LIVE,
        accepted_checkpoints,
    ).satisfied


def resume_available(unsatisfied_dependencies: Collection[Dependency], replacement_resolved: bool) -> bool:
    """The one predicate under which held work may resume and every hold sentence says someone can resume it.

    Every accepted dependency must be satisfied and no current planned replacement may still withhold the work.
    """

    return not unsatisfied_dependencies and replacement_resolved


def replacement_withholds_resume(
    status: PlannedReplacementStatus, replacement_cost: str, accepted_cost: str | None
) -> bool:
    """A current planned replacement withholds resume until a disposition accepts its exact current cost."""

    return status == PlannedReplacementStatus.CURRENT and accepted_cost != replacement_cost


class WorkState(Enum):
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"


type LiveAttemptItemState = Literal[WorkState.ACTIVE, WorkState.PAUSED, WorkState.BLOCKED, WorkState.REVIEW]
"""A work item state that can own a live attempt; a ready item owns none."""


class AttemptState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


class CloseOutcome(Enum):
    DONE = "done"
    DROPPED = "dropped"
    SUPERSEDED = "superseded"


class DirectCloseOutcome(Enum):
    DONE = "done"
    SUPERSEDED = "superseded"


class Timing(Enum):
    MUST_NOW = "must-now"
    CHEAPER_NOW = "cheaper-now"
    SAFE_TO_DEFER = "safe-to-defer"


class CheckoutPolicy(Enum):
    MAIN = "main"
    ISOLATED = "isolated"
    COORDINATOR_SELECTED = "coordinator-selected"


class CheckoutSelection(Enum):
    MAIN = "main"
    ISOLATED = "isolated"


class SizeClass(Enum):
    SMALL = "small"
    STANDARD = "standard"
    CAMPAIGN = "campaign"

    @property
    def requires_cross_boundary_brief_review(self) -> bool:
        match self:
            case SizeClass.SMALL:
                return False
            case SizeClass.STANDARD | SizeClass.CAMPAIGN:
                return True
            case _ as unreachable:
                assert_never(unreachable)


class ArchitectureImpact(Enum):
    NONE = "none"
    ACKNOWLEDGED_LIMITATION = "acknowledged-limitation"
    NEW_DECISION = "new-decision"


class ExternalEffects(Enum):
    NONE = "none"
    PUBLISH = "publish"
    MERGE = "merge"
    EXTERNAL_SERVICE = "external-service"


@dataclass(frozen=True, slots=True)
class RouteFacts:
    size_class: SizeClass
    architecture_impact: ArchitectureImpact
    external_effects: ExternalEffects
    declares_checkpoints: bool


class ObligationDeferralPolicy(Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class WorkObligation:
    obligation_id: ObligationId
    statement: str
    deferral_policy: ObligationDeferralPolicy


@dataclass(frozen=True, slots=True)
class ObligationOrigin:
    item_id: WorkItemId
    definition_revision: int
    obligation_id: ObligationId


@dataclass(frozen=True, slots=True)
class CarriedWorkObligation:
    obligation_id: ObligationId
    statement: str
    deferral_policy: ObligationDeferralPolicy
    origin: ObligationOrigin


type DefinitionObligation = WorkObligation | CarriedWorkObligation


class ArtifactKind(Enum):
    REQUIREMENTS = "requirements"
    BRIEF = "brief"
    RESULT = "result"
    EVIDENCE = "evidence"


class ProposalRelationKind(Enum):
    INDEPENDENT = "independent"
    PREREQUISITE = "prerequisite"
    FOLLOW_UP = "follow-up"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    CLARIFICATION = "clarification"
    PLANNED_REPLACEMENT = "planned-replacement"


class PlannedReplacementStatus(Enum):
    CURRENT = "current"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class PlannedReplacement:
    affected_item: WorkItemId
    relation_revision: int
    replacement_item: WorkItemId
    replacement_cost: str
    status: PlannedReplacementStatus
    recorded_by: TaskId
    recorded_at: datetime


def planned_replacement_revision(value: PlannedReplacement) -> int:
    return value.relation_revision


@dataclass(frozen=True, slots=True)
class ReplacementDisposition:
    affected_item: WorkItemId
    relation_revision: int
    rationale: str
    accepted_cost: str
    recorded_by: TaskId
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class IndependentProposalRelation:
    work_item_id: None = None
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.INDEPENDENT)


@dataclass(frozen=True, slots=True)
class PrerequisiteProposalRelation:
    work_item_id: WorkItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.PREREQUISITE)


@dataclass(frozen=True, slots=True)
class FollowUpProposalRelation:
    dependency: Dependency
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.FOLLOW_UP)

    @property
    def work_item_id(self) -> WorkItemId:
        return dependency_item(self.dependency)


@dataclass(frozen=True, slots=True)
class DuplicateProposalRelation:
    work_item_id: WorkItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.DUPLICATE)


@dataclass(frozen=True, slots=True)
class ContradictionProposalRelation:
    work_item_id: WorkItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.CONTRADICTION)


@dataclass(frozen=True, slots=True)
class ClarificationProposalRelation:
    work_item_id: None = None
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.CLARIFICATION)


@dataclass(frozen=True, slots=True)
class PlannedReplacementProposalRelation:
    work_item_id: WorkItemId
    replacement_cost: str
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.PLANNED_REPLACEMENT)


type ProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
    | PlannedReplacementProposalRelation
)


class ProposalDispositionKind(Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    RETURNED = "returned"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MergedProposalDisposition:
    target: WorkItemId
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.MERGED)


@dataclass(frozen=True, slots=True)
class RejectedProposalDisposition:
    reason: str
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.REJECTED)


type ProposalDisposition = MergedProposalDisposition | RejectedProposalDisposition


@dataclass(frozen=True, slots=True)
class WaitingForItemsToFinish:
    """Held work continues once these accepted dependencies finish; Pinboard detects their completion."""

    items: tuple[WorkItemId, ...]


@dataclass(frozen=True, slots=True)
class WaitingForHumanDecision:
    """Held work continues once a human answers this question."""

    question: str


@dataclass(frozen=True, slots=True)
class WaitingForOutsideCondition:
    """Held work continues once this stated condition outside Pinboard becomes true; Pinboard does not check it."""

    condition: str


type Unblocker = WaitingForItemsToFinish | WaitingForHumanDecision | WaitingForOutsideCondition


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: WorkItemId
    state: WorkState
    timing: str | None
    depends_on: tuple[Dependency, ...]
    attempt: AttemptId | None
    source: str | None
    next_action: str | None
    notes: str | None
    queue_position: int | None
    outcome_evidence: str | None = None
    unblocker: Unblocker | None = field(kw_only=True)
    """What the held item waits for; present exactly while the item is paused or blocked."""


@dataclass(frozen=True, slots=True)
class ResumeInput:
    brief_artifact_ref_id: ArtifactRefId | None = None


@dataclass(frozen=True, slots=True)
class PauseDecision:
    decision_id: DecisionId
    question: str
    options: tuple[str, ...]
    recommendation: str
    unblocks: str
    deferral_cost: str


@dataclass(frozen=True, slots=True)
class DecisionBatchInput:
    reason: str
    decisions: tuple[PauseDecision, ...]


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    decision_id: DecisionId
    human_decision: str


@dataclass(frozen=True, slots=True)
class DecisionDeferral:
    decision_id: DecisionId
    human_decision: str
    reopen_condition: str


type DecisionResolution = DecisionAnswer | DecisionDeferral


@dataclass(frozen=True, slots=True)
class ResolveDecisionsInput:
    brief_artifact_ref_id: ArtifactRefId | None
    resolutions: tuple[DecisionResolution, ...]


@dataclass(frozen=True, slots=True)
class PendingDecisionBatch:
    attempt_id: AttemptId
    pause_history_id: HistoryId
    decisions: tuple[PauseDecision, ...]


@dataclass(frozen=True, slots=True)
class ResolvedPauseDecisions:
    pause_history_id: HistoryId
    resolutions: tuple[DecisionResolution, ...]


@dataclass(frozen=True, slots=True)
class RebindAttemptInput:
    branch: str
    base_revision: str
    brief_artifact_ref_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class ActivateInput:
    attempt: AttemptId
    branch: str
    base_revision: str
    owner: str
    brief_artifact_ref_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class SubmitReviewInput:
    candidate: CandidateId


@dataclass(frozen=True, slots=True)
class DeclaredSubmitReviewInput:
    candidate: CandidateId
    excluded_untracked_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReasonInput:
    reason: str


@dataclass(frozen=True, slots=True)
class BlockInput:
    unblocker: Unblocker


@dataclass(frozen=True, slots=True)
class PlainPauseInput:
    reason: str
    unblocker: Unblocker


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    evidence: str


class CompletionPackageDisposition(Enum):
    REUSED = "reused"
    REVALIDATED = "revalidated"


@dataclass(frozen=True, slots=True)
class CoveredCompletionPackageInput:
    history_id: HistoryId
    package_sha256: str
    disposition: CompletionPackageDisposition
    evidence: str


@dataclass(frozen=True, slots=True)
class LandedCompletionInput:
    target: str
    retain_local_resources: bool


@dataclass(frozen=True, slots=True)
class CoveredCompleteInput:
    human_decision: str
    candidate: CandidateId
    evidence: str
    reviewer_task_id: TaskId
    result_sha256: str
    review_sha256: str
    packages: tuple[CoveredCompletionPackageInput, ...]
    landed: LandedCompletionInput | None


@dataclass(frozen=True, slots=True)
class AcceptCheckpointInput:
    checkpoint: CheckpointId
    candidate: CandidateId
    evidence: str


@dataclass(frozen=True, slots=True)
class AcceptReviewAndContinueInput:
    candidate: CandidateId
    evidence: str


class AmendmentClass(Enum):
    """The closed kinds of human-approved incidental repair an attempt amendment may record."""

    STALE_DOCUMENTATION = "stale-documentation"
    FLAKY_OR_BROKEN_TEST = "flaky-or-broken-test"
    LINT_OR_TOOLING = "lint-or-tooling"
    COMMENT_OR_NAME = "comment-or-name"
    MECHANICAL_BASELINE_CLEANUP = "mechanical-baseline-cleanup"


@dataclass(frozen=True, slots=True)
class ReviewBase:
    """An earlier reviewed submission an amendment-only delta review may compare against.

    The candidate, its accepted snapshot and the ready candidate review recorded for that submission.
    """

    candidate: CandidateId
    snapshot_artifact_ref_id: ArtifactRefId
    ready_review_artifact_ref_id: ArtifactRefId


@dataclass(frozen=True, slots=True)
class ClearedSubmission:
    """The protected submission an amendment on a review attempt clears, observed before the decision.

    The ready review is that submission's own valid ready candidate review, when one was recorded.
    """

    candidate: CandidateId
    snapshot_artifact_ref_id: ArtifactRefId
    ready_review_artifact_ref_id: ArtifactRefId | None


@dataclass(frozen=True, slots=True)
class RecordAmendmentInput:
    """The caller's amendment plus the review facts the transition adapter observed for the attempt.

    The carried base is the review base of the attempt's latest amendment under its current brief when no return,
    rebind or acceptance followed it; the attempt subject revision the caller selected binds both observations.
    """

    amendment_class: AmendmentClass
    change: str
    reason: str
    human_decision: str
    cleared_submission: ClearedSubmission | None
    carried_base: ReviewBase | None


@dataclass(frozen=True, slots=True)
class AttemptAmendment:
    """One recorded amendment to an attempt's contract, bound to the accepted brief it was recorded against."""

    amendment_class: AmendmentClass
    change: str
    reason: str
    human_decision: str
    brief_artifact_ref_id: ArtifactRefId
    cleared_candidate: CandidateId | None
    cleared_snapshot_artifact_ref_id: ArtifactRefId | None
    review_base: ReviewBase | None


@dataclass(frozen=True, slots=True)
class RecordPlannedReplacementInput:
    expected_relation_revision: int
    replacement_item: WorkItemId
    replacement_cost: str
    status: PlannedReplacementStatus
    recorded_by: TaskId


@dataclass(frozen=True, slots=True)
class RetainTemporarilyInput:
    relation_revision: int
    rationale: str
    accepted_cost: str
    recorded_by: TaskId


@dataclass(frozen=True, slots=True)
class CloseInput:
    outcome: DirectCloseOutcome
    reason: str
    human_decision: str


@dataclass(frozen=True, slots=True)
class MergeProposalInput:
    target: WorkItemId


@dataclass(frozen=True, slots=True)
class WorkItemDefinition:
    title: str
    objective: str
    hypothesis: str
    evidence: tuple[str, ...]
    scope: tuple[str, ...]
    non_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[Dependency, ...]
    effect: str
    unlock: str
    checkout_policy: CheckoutPolicy
    obligations: tuple[DefinitionObligation, ...]
    checkpoints: tuple[CheckpointId, ...]
    route_facts: RouteFacts | None


@dataclass(frozen=True, slots=True)
class ReviseWorkItemDefinitionInput:
    expected_revision: int
    expected_digest: str
    source_task: TaskId
    reason: str
    definition: WorkItemDefinition


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_ref_id: ArtifactRefId
    kind: ArtifactKind


@dataclass(frozen=True, slots=True)
class DefinitionAnchor:
    work_item_id: WorkItemId
    revision: int
    digest: str
    definition: WorkItemDefinition


@dataclass(frozen=True, slots=True)
class DefinitionDependencies:
    work_item_id: WorkItemId
    dependencies: tuple[Dependency, ...]


@dataclass(frozen=True, slots=True)
class CommandAttemptAuthority:
    host_epoch: int
    work_item_id: WorkItemId
    item_subject_revision: str
    attempt: AttemptId
    attempt_subject_revision: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PreparationCommandAuthority:
    host_epoch: int
    work_item_id: WorkItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    attempt: AttemptId
    work_item_id: WorkItemId
    lease_id: LeaseId | None
    generation: int


@dataclass(frozen=True, slots=True)
class PreparationAuthority:
    work_item_id: WorkItemId
    definition_revision: int
    definition_digest: str
    lease_id: LeaseId | None
    generation: int


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal: ProposalId
    revision: str
    created_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    relation: ProposalRelation
    effect: str
    unlock: str
    urgency_evidence: str
    evidence: tuple[str, ...] = ()
    freshness: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt: AttemptId
    work_item_id: WorkItemId
    state: AttemptState
    accepted_scope_revision: int | None = None
    accepted_scope_digest: str | None = None
    protected_candidate_revision: CandidateId | None = None
    brief_artifact_ref_id: ArtifactRefId | None = None
    pause_reason: str | None = field(kw_only=True)
    pending_decision_batch: PendingDecisionBatch | None = field(kw_only=True)
    """The human reason recorded by the latest pause, or its rebind carry-forward, while the attempt is paused."""


@dataclass(frozen=True, slots=True)
class ProjectAttemptActionContext:
    work_item_id: WorkItemId
    item_subject_revision: str
    item_state: LiveAttemptItemState
    attempt: AttemptId
    attempt_subject_revision: str
    attempt_record: AttemptRecord | None
    current_definition_revision: int | None
    current_definition_digest: str | None
    unsatisfied_dependencies: tuple[Dependency, ...]
    revision_available: bool
    current_replacement_revision: int | None
    replacement_resolved: bool


@dataclass(frozen=True, slots=True)
class SubjectRevision:
    subject: WorkItemId | AttemptId | ProposalId
    revision: str
