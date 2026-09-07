from dataclasses import dataclass
from datetime import datetime

from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import ItemId, TaskId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class DefinitionRevisionDecision:
    item: ItemId
    revision: int
    before_digest: str
    after_digest: str
    definition: work_models.WorkItemDefinition
    source_task: TaskId
    reason: str
    decided_at: datetime


def introduces_dependency_cycle(snapshot: LedgerSnapshot, item: ItemId, dependencies: tuple[ItemId, ...]) -> bool:
    pending = list(dependencies)
    visited: set[ItemId] = set()
    while pending:
        dependency = pending.pop()
        if dependency == item:
            return True
        if dependency in visited:
            continue
        visited.add(dependency)
        anchor = snapshot.definition(dependency)
        if anchor is not None:
            pending.extend(anchor.definition.dependencies)
    return False


def decide_definition_revision(
    snapshot: LedgerSnapshot,
    item: ItemId,
    value: work_models.ReviseItemDefinitionInput,
    now: datetime,
) -> DecisionResult[DefinitionRevisionDecision]:
    if snapshot.item(item) is None:
        if item in snapshot.history_items:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
                "A terminal work item cannot be revised.",
                None,
            )
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item}' does not exist.", None)
    if value.item_id != item:
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The revision payload item does not match the selected action.",
            None,
        )
    current = snapshot.definition(item)
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
    known_items = {*snapshot.items_by_id(), *snapshot.history_items}
    if any(dependency not in known_items for dependency in value.definition.dependencies):
        return DecisionFailure(
            DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            "Definition dependencies must name existing work items.",
            None,
        )
    if introduces_dependency_cycle(snapshot, item, value.definition.dependencies):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
            "Definition dependencies must not introduce a cycle.",
            None,
        )
    return DefinitionRevisionDecision(
        item,
        current.revision + 1,
        current.digest,
        digest,
        value.definition,
        value.source_task,
        value.reason,
        now,
    )
