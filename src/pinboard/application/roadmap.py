"""Derive one global dependency layout and unique delivery totals from selected ledger facts."""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import assert_never

from pinboard.application import stored_state
from pinboard.domain import initiatives
from pinboard.domain.identifiers import WorkItemId


@dataclass(frozen=True, slots=True)
class Item:
    item_id: WorkItemId
    title: str
    state: stored_state.StoredWorkItemState
    dependencies: tuple[WorkItemId, ...]
    queue_position: int | None


@dataclass(frozen=True, slots=True)
class Facts:
    project_revision: int
    initiatives: tuple[initiatives.Initiative, ...]
    items: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class Wave:
    number: int


@dataclass(frozen=True, slots=True)
class Blocked:
    path: tuple[WorkItemId, ...]


@dataclass(frozen=True, slots=True)
class Unknown:
    condition: str


type Placement = Wave | Blocked | Unknown


@dataclass(frozen=True, slots=True)
class Counts:
    delivered: int
    cancelled: int
    superseded: int
    unfinished: int


@dataclass(frozen=True, slots=True)
class Projection:
    facts: Facts
    placements: Mapping[WorkItemId, Placement]
    current_members: frozenset[WorkItemId]
    unassigned: tuple[WorkItemId, ...]
    external_prerequisites: tuple[WorkItemId, ...]
    totals: Counts


def is_unfinished(state: stored_state.StoredWorkItemState) -> bool:
    match state:
        case (
            stored_state.StoredWorkItemState.DONE
            | stored_state.StoredWorkItemState.DROPPED
            | stored_state.StoredWorkItemState.SUPERSEDED
        ):
            return False
        case (
            stored_state.StoredWorkItemState.INTAKE
            | stored_state.StoredWorkItemState.READY
            | stored_state.StoredWorkItemState.ACTIVE
            | stored_state.StoredWorkItemState.PAUSED
            | stored_state.StoredWorkItemState.BLOCKED
            | stored_state.StoredWorkItemState.DEFERRED
            | stored_state.StoredWorkItemState.REVIEW
        ):
            return True
        case _ as unreachable:
            assert_never(unreachable)


def counts(items: tuple[Item, ...]) -> Counts:
    return Counts(
        sum(item.state == stored_state.StoredWorkItemState.DONE for item in items),
        sum(item.state == stored_state.StoredWorkItemState.DROPPED for item in items),
        sum(item.state == stored_state.StoredWorkItemState.SUPERSEDED for item in items),
        sum(is_unfinished(item.state) for item in items),
    )


def project(facts: Facts) -> Projection:  # noqa: C901, PLR0912 - one global graph classification and topological layout
    by_id = {item.item_id: item for item in facts.items}
    members = frozenset(
        member.item_id
        for initiative in facts.initiatives
        for member in initiative.memberships
        if member.removed is None
    )
    unassigned = tuple(
        item.item_id for item in facts.items if item.queue_position is not None and item.item_id not in members
    )
    included = members | frozenset(unassigned)
    unfinished = {item.item_id: item for item in facts.items if is_unfinished(item.state)}
    dependents: dict[WorkItemId, list[WorkItemId]] = {item_id: [] for item_id in by_id}
    invalid: dict[WorkItemId, Blocked | Unknown] = {}
    for item in unfinished.values():
        for prerequisite in item.dependencies:
            dependency = by_id.get(prerequisite)
            if dependency is None:
                invalid[item.item_id] = Unknown(f"Missing prerequisite '{prerequisite}' for '{item.item_id}'.")
            else:
                dependents[prerequisite].append(item.item_id)
                if dependency.state == stored_state.StoredWorkItemState.DROPPED:
                    invalid[item.item_id] = Blocked((item.item_id, prerequisite))
    blocked = deque(invalid)
    while blocked:
        item_id = blocked.popleft()
        placement = invalid[item_id]
        for dependent in dependents[item_id]:
            if dependent in invalid:
                continue
            match placement:
                case Blocked(path=path):
                    invalid[dependent] = Blocked((dependent, *path))
                case Unknown(condition=condition):
                    invalid[dependent] = Unknown(condition)
                case _ as unreachable:
                    assert_never(unreachable)
            blocked.append(dependent)
    placements: dict[WorkItemId, Placement] = dict(invalid)
    remaining = {item_id: item for item_id, item in unfinished.items() if item_id not in placements}
    indegree = {
        item_id: sum(dependency in remaining for dependency in item.dependencies) for item_id, item in remaining.items()
    }
    ready = deque(item_id for item_id, degree in indegree.items() if degree == 0)
    while ready:
        item_id = ready.popleft()
        levels = [
            placement.number
            for dependency in remaining[item_id].dependencies
            if isinstance(placement := placements.get(dependency), Wave)
        ]
        placements[item_id] = Wave(1 + max(levels, default=0))
        for dependent in dependents[item_id]:
            if dependent in indegree:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
    for item_id in remaining:
        if item_id not in placements:
            placements[item_id] = Unknown(
                f"Cyclic or invalid dependency graph reaches '{item_id}' through {', '.join(remaining[item_id].dependencies)}."
            )
    prerequisite_ids = frozenset(dependency for item in unfinished.values() for dependency in item.dependencies)
    external_prerequisites = tuple(
        item.item_id for item in facts.items if item.item_id in prerequisite_ids and item.item_id not in included
    )
    return Projection(
        facts,
        MappingProxyType(placements),
        members,
        unassigned,
        external_prerequisites,
        counts(tuple(item for item in facts.items if item.item_id in included)),
    )


def _dependency_order(value: stored_state.ItemDependency) -> int:
    return value.position


def item_order(value: Item | stored_state.StoredWorkItem) -> tuple[bool, int, str]:
    return value.queue_position is None, value.queue_position or 0, value.item_id


def from_state(state: stored_state.StoredWorkState) -> Projection:
    definitions: dict[WorkItemId, stored_state.ItemDefinitionRevision] = {}
    for revision in state.lifecycle.definition_revisions:
        current = definitions.get(revision.item_id)
        if current is None or current.revision < revision.revision:
            definitions[revision.item_id] = revision
    dependencies: dict[WorkItemId, list[WorkItemId]] = {}
    for dependency in sorted(state.lifecycle.dependencies, key=_dependency_order):
        dependencies.setdefault(dependency.item_id, []).append(dependency.dependency_id)
    selected = {item.item_id for item in state.lifecycle.work_items if item.queue_position is not None}
    selected.update(member.item_id for initiative in state.initiatives for member in initiative.memberships)
    items = {item.item_id: item for item in state.lifecycle.work_items}
    pending = list(selected)
    while pending:
        item_id = pending.pop()
        item = items.get(item_id)
        if item is None or not is_unfinished(item.state):
            continue
        for dependency in dependencies.get(item_id, ()):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return project(
        Facts(
            state.lifecycle.project.revision,
            state.initiatives,
            tuple(
                Item(
                    item.item_id,
                    definitions[item.item_id].definition.title,
                    item.state,
                    tuple(dependencies.get(item.item_id, ())) if is_unfinished(item.state) else (),
                    item.queue_position,
                )
                for item in sorted(state.lifecycle.work_items, key=item_order)
                if item.item_id in selected
            ),
        )
    )
