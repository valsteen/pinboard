from dataclasses import dataclass
from datetime import datetime

from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId, ProposalId, TaskId


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
    position: int | None = None


@dataclass(frozen=True, slots=True)
class IntakeWorkItem:
    item_id: ItemId
    position: int
    dependencies: tuple[ItemId, ...]
    definition_digest: str
    definition: work_models.WorkItemDefinition


@dataclass(frozen=True, slots=True)
class PrerequisiteDependencyChange:
    item_id: ItemId
    dependency_id: ItemId
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
    intake_item: IntakeWorkItem
    prerequisite_change: PrerequisiteDependencyChange | None
    planned_replacement: work_models.PlannedReplacement | None
    evidence: tuple[str, ...]
    freshness: tuple[str, ...]
