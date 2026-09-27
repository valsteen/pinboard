from dataclasses import dataclass
from datetime import datetime

from pinboard.domain import work_models
from pinboard.domain.identifiers import ProposalId, TaskId, WorkItemId


@dataclass(frozen=True, slots=True)
class ProposalIntake:
    proposal_id: ProposalId
    created_at: datetime
    source_task_id: TaskId
    user_label: str
    trigger: str
    why_it_matters: str
    effect: str
    unlock: str
    relation: work_models.ProposalRelation
    urgency_evidence: str
    evidence: tuple[str, ...]
    freshness_assumptions: tuple[str, ...]
    checkout_policy: work_models.CheckoutPolicy
    obligations: tuple[work_models.WorkObligation, ...]
    position: int | None = None


@dataclass(frozen=True, slots=True)
class ReadyProposalWorkItem:
    work_item_id: WorkItemId
    position: int
    dependencies: tuple[WorkItemId, ...]
    definition_digest: str
    definition: work_models.WorkItemDefinition


@dataclass(frozen=True, slots=True)
class PrerequisiteDependencyChange:
    work_item_id: WorkItemId
    dependency_id: WorkItemId
    position: int
    definition_revision: int
    definition_digest_before: str
    definition_digest_after: str
    definition_after: work_models.WorkItemDefinition


@dataclass(frozen=True, slots=True)
class CreateProposalOperation:
    intake: ProposalIntake


@dataclass(frozen=True, slots=True)
class ProposalCreationDecision:
    proposal: ProposalIntake
    ready_item: ReadyProposalWorkItem
    prerequisite_change: PrerequisiteDependencyChange | None
    planned_replacement: work_models.PlannedReplacement | None
    evidence: tuple[str, ...]
    freshness: tuple[str, ...]
