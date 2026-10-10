from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NewType

from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HistoryId,
    HostId,
    LeaseId,
    ProposalId,
    TaskId,
    WorkItemId,
)

ObligationId = NewType("ObligationId", str)

CanonicalJson = NewType("CanonicalJson", bytes)


class WorkState(Enum):
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    REVIEW = "review"


class AttemptState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


class CloseOutcome(Enum):
    DONE = "done"
    DROPPED = "dropped"


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


class ObligationDeferralPolicy(Enum):
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class WorkObligation:
    obligation_id: ObligationId
    statement: str
    deferral_policy: ObligationDeferralPolicy


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
    work_item_id: WorkItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.FOLLOW_UP)


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
class WorkItem:
    work_item_id: WorkItemId
    state: WorkState
    timing: str | None
    depends_on: tuple[WorkItemId, ...]
    attempt: AttemptId | None
    source: str | None
    next_action: str | None
    notes: str | None
    queue_position: int | None
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeInput:
    brief_artifact_ref_id: ArtifactRefId | None = None


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
    reason: str
    depends_on: tuple[WorkItemId, ...] = ()


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
class CoveredCompleteInput:
    candidate: CandidateId
    evidence: str
    reviewer_task_id: TaskId
    result_sha256: str
    review_sha256: str
    packages: tuple[CoveredCompletionPackageInput, ...]


@dataclass(frozen=True, slots=True)
class AcceptCheckpointInput:
    checkpoint: CheckpointId
    candidate: CandidateId
    evidence: str


@dataclass(frozen=True, slots=True)
class AcceptReviewAndContinueInput:
    candidate: CandidateId
    evidence: str


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
    outcome: CloseOutcome
    reason: str
    human_decision: str


@dataclass(frozen=True, slots=True)
class DeferInput:
    timing: Timing
    reopen_condition: str


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
    dependencies: tuple[WorkItemId, ...]
    effect: str
    unlock: str
    checkout_policy: CheckoutPolicy
    obligations: tuple[WorkObligation, ...]


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
    dependencies: tuple[WorkItemId, ...]


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
    """The human reason recorded by the latest pause, or its rebind carry-forward, while the attempt is paused."""


@dataclass(frozen=True, slots=True)
class ProjectAttemptActionContext:
    work_item_id: WorkItemId
    item_subject_revision: str
    item_state: WorkState
    attempt: AttemptId
    attempt_subject_revision: str
    attempt_record: AttemptRecord | None
    current_definition_revision: int | None
    current_definition_digest: str | None
    live_dependencies: tuple[WorkItemId, ...]
    revision_available: bool
    current_replacement_revision: int | None
    replacement_resolved: bool


@dataclass(frozen=True, slots=True)
class SubjectRevision:
    subject: WorkItemId | AttemptId | ProposalId
    revision: str
