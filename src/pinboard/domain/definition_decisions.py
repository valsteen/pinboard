from dataclasses import dataclass
from datetime import datetime

from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import TaskId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class DefinitionRevisionDecision:
    work_item_id: WorkItemId
    revision: int
    before_digest: str
    after_digest: str
    definition: work_models.WorkItemDefinition
    source_task: TaskId
    reason: str
    decided_at: datetime


def introduces_dependency_cycle(
    snapshot: LedgerSnapshot, work_item_id: WorkItemId, dependencies: tuple[WorkItemId, ...]
) -> bool:
    pending = list(dependencies)
    visited: set[WorkItemId] = set()
    while pending:
        dependency = pending.pop()
        if dependency == work_item_id:
            return True
        if dependency in visited:
            continue
        visited.add(dependency)
        recorded = snapshot.recorded_dependencies(dependency)
        if recorded is not None:
            pending.extend(recorded)
    return False


def decide_definition_revision(
    snapshot: LedgerSnapshot,
    work_item_id: WorkItemId,
    value: work_models.ReviseWorkItemDefinitionInput,
    now: datetime,
) -> DecisionResult[DefinitionRevisionDecision]:
    if snapshot.work_item(work_item_id) is None:
        if work_item_id in snapshot.history_items:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
                "A terminal work item cannot be revised.",
                None,
            )
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{work_item_id}' does not exist.", None)
    current = snapshot.definition(work_item_id)
    if current is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "The work item has no current definition.",
            None,
        )
    if (value.expected_revision, value.expected_digest) != (current.revision, current.digest):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            "The expected definition revision and digest are stale.",
            None,
        )
    digest = work_item_definition_digest(value.definition)
    if isinstance(digest, DecisionFailure):
        return digest
    known_items = {*snapshot.work_items_by_id(), *snapshot.history_items}
    if any(dependency not in known_items for dependency in value.definition.dependencies):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Definition dependencies must name existing work items.",
            None,
        )
    if introduces_dependency_cycle(snapshot, work_item_id, value.definition.dependencies):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
            "Definition dependencies must not introduce a cycle.",
            None,
        )
    return DefinitionRevisionDecision(
        work_item_id,
        current.revision + 1,
        current.digest,
        digest,
        value.definition,
        value.source_task,
        value.reason,
        now,
    )
