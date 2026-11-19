from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pinboard.domain import work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    RetryDisposition,
)
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import TaskId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot

type DefinitionRevisionCode = Literal[
    DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_STALE,
    DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
    DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
    DecisionFailureCode.ITEM_NOT_FOUND,
]


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


type DependencyAdditionCode = Literal[
    DecisionFailureCode.ITEM_DEFINITION_INVALID,
    DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
]


def validate_added_dependencies(
    work_item_id: WorkItemId,
    item: work_models.WorkItem | None,
    previous: tuple[work_models.Dependency, ...],
    requested: tuple[work_models.Dependency, ...],
) -> DecisionFailure[DependencyAdditionCode] | None:
    added = tuple(value for value in requested if value not in previous)
    if not added:
        return None
    targets = tuple(
        str(value) if isinstance(value, str) else f"{value.item_id} checkpoint {value.checkpoint_id}" for value in added
    )
    if item is not None and item.has_started:
        code: DependencyAdditionCode = DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID
        message = (
            f"Started item '{work_item_id}' cannot add prerequisites: {', '.join(targets)}. "
            "Continue independent work and check the current owners of unfinished obligations. "
            "Ask the human before assigning uncovered work to a whole-item follow-up."
        )
    elif any(isinstance(value, work_models.CheckpointDependency) for value in added):
        code = DecisionFailureCode.ITEM_DEFINITION_INVALID
        message = (
            f"Item '{work_item_id}' can add only whole-item prerequisites: {', '.join(targets)}. "
            "Inspect the current definition and use the exact producer item ID for each new prerequisite."
        )
    else:
        return None
    return DecisionFailure(
        code,
        message,
        FailureDetails(
            observed=(
                FailureFact("item_id", str(work_item_id)),
                *(FailureFact("attempted_prerequisite", target) for target in targets),
            ),
            mismatches=(),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def introduces_dependency_cycle(
    snapshot: LedgerSnapshot, work_item_id: WorkItemId, dependencies: tuple[work_models.Dependency, ...]
) -> bool:
    pending = [work_models.dependency_item(value) for value in dependencies]
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
            pending.extend(work_models.dependency_item(value) for value in recorded)
    return False


def decide_definition_revision(
    snapshot: LedgerSnapshot,
    work_item_id: WorkItemId,
    value: work_models.ReviseWorkItemDefinitionInput,
    now: datetime,
) -> DecisionResult[DefinitionRevisionDecision, DefinitionRevisionCode]:
    item = snapshot.work_item(work_item_id)
    if item is None:
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
    if current.definition.route_facts is not None and value.definition.route_facts is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            "A current classified definition cannot discard its recorded route facts. Supply the complete explicit classification in the new definition.",
            None,
        )
    removed_checkpoints = tuple(
        str(checkpoint)
        for checkpoint in current.definition.checkpoints
        if checkpoint not in value.definition.checkpoints
    )
    if removed_checkpoints:
        removed = ", ".join(removed_checkpoints)
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Definition revision cannot remove declared checkpoints: {removed}.",
            None,
        )
    if (
        failure := validate_added_dependencies(
            work_item_id, item, current.definition.dependencies, value.definition.dependencies
        )
    ) is not None:
        return failure
    digest = work_item_definition_digest(value.definition)
    if isinstance(digest, DecisionFailure):
        return digest
    known_items = {*snapshot.work_items_by_id(), *snapshot.history_items}
    if any(work_models.dependency_item(dependency) not in known_items for dependency in value.definition.dependencies):
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
