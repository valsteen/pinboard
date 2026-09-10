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
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)

CanonicalJson = NewType("CanonicalJson", bytes)


class WorkState(Enum):
    INTAKE = "intake"
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


class AcceptedProposalState(Enum):
    INTAKE = "intake"
    READY = "ready"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class CloseOutcome(Enum):
    DONE = "done"
    DROPPED = "dropped"


class Timing(Enum):
    MUST_NOW = "must-now"
    CHEAPER_NOW = "cheaper-now"
    SAFE_TO_DEFER = "safe-to-defer"


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


@dataclass(frozen=True, slots=True)
class IndependentProposalRelation:
    item: None = None
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.INDEPENDENT)


@dataclass(frozen=True, slots=True)
class PrerequisiteProposalRelation:
    item: ItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.PREREQUISITE)


@dataclass(frozen=True, slots=True)
class FollowUpProposalRelation:
    item: ItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.FOLLOW_UP)


@dataclass(frozen=True, slots=True)
class DuplicateProposalRelation:
    item: ItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.DUPLICATE)


@dataclass(frozen=True, slots=True)
class ContradictionProposalRelation:
    item: ItemId
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.CONTRADICTION)


@dataclass(frozen=True, slots=True)
class ClarificationProposalRelation:
    item: None = None
    kind: ProposalRelationKind = field(init=False, default=ProposalRelationKind.CLARIFICATION)


type ProposalRelation = (
    IndependentProposalRelation
    | PrerequisiteProposalRelation
    | FollowUpProposalRelation
    | DuplicateProposalRelation
    | ContradictionProposalRelation
    | ClarificationProposalRelation
)


class ProposalDispositionKind(Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    RETURNED = "returned"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AcceptedProposalDisposition:
    target: ItemId
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.ACCEPTED)


@dataclass(frozen=True, slots=True)
class MergedProposalDisposition:
    target: ItemId
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.MERGED)


@dataclass(frozen=True, slots=True)
class ReturnedProposalDisposition:
    reason: str
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.RETURNED)


@dataclass(frozen=True, slots=True)
class RejectedProposalDisposition:
    reason: str
    disposed_at: datetime
    kind: ProposalDispositionKind = field(init=False, default=ProposalDispositionKind.REJECTED)


type ProposalDisposition = (
    AcceptedProposalDisposition | MergedProposalDisposition | ReturnedProposalDisposition | RejectedProposalDisposition
)


@dataclass(frozen=True, slots=True)
class WorkItem:
    item: ItemId
    state: WorkState
    timing: str | None
    depends_on: tuple[ItemId, ...]
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
    attempt: AttemptId
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
class ReasonInput:
    reason: str


@dataclass(frozen=True, slots=True)
class BlockInput:
    reason: str
    depends_on: tuple[ItemId, ...] = ()


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
class CloseInput:
    outcome: CloseOutcome
    reason: str


@dataclass(frozen=True, slots=True)
class DeferInput:
    timing: Timing
    reopen_condition: str


@dataclass(frozen=True, slots=True)
class AcceptProposalInput:
    item: ItemId
    state: AcceptedProposalState
    next_action: str
    timing: Timing | None = None
    depends_on: tuple[ItemId, ...] = ()


@dataclass(frozen=True, slots=True)
class MergeProposalInput:
    target: ItemId


@dataclass(frozen=True, slots=True)
class WorkItemDefinition:
    title: str
    objective: str
    hypothesis: str
    evidence: tuple[str, ...]
    scope: tuple[str, ...]
    non_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[ItemId, ...]
    effect: str
    unlock: str


@dataclass(frozen=True, slots=True)
class ReviseItemDefinitionInput:
    item_id: ItemId
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
    item: ItemId
    revision: int
    digest: str
    definition: WorkItemDefinition


@dataclass(frozen=True, slots=True)
class CommandAttemptAuthority:
    host_epoch: int
    item: ItemId
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
    item: ItemId
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
    item: ItemId
    lease_id: LeaseId | None
    generation: int


@dataclass(frozen=True, slots=True)
class PreparationAuthority:
    item: ItemId
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
    item: ItemId
    state: AttemptState
    accepted_scope_revision: int | None = None
    accepted_scope_digest: str | None = None
    protected_candidate_revision: CandidateId | None = None
    brief_artifact_ref_id: ArtifactRefId | None = None


@dataclass(frozen=True, slots=True)
class ProjectAttemptActionContext:
    item: ItemId
    item_subject_revision: str
    item_state: WorkState
    attempt: AttemptId
    attempt_subject_revision: str
    attempt_record: AttemptRecord | None
    current_definition_revision: int | None
    current_definition_digest: str | None
    live_dependencies: tuple[ItemId, ...]
    revision_available: bool


@dataclass(frozen=True, slots=True)
class SubjectRevision:
    subject: ItemId | AttemptId | ProposalId
    revision: str
