"""Recheck verified checkpoint targets against locked producer facts.

A revision verifies only the checkpoint dependencies that are new relative to the dependent's current definition.
"""

from dataclasses import dataclass
from typing import assert_never

from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class DependencyDefinitionPin:
    target: work_models.CheckpointDependency
    definition_revision: int
    definition_digest: str


@dataclass(frozen=True, slots=True)
class DependencyBriefPin:
    target: work_models.CheckpointDependency
    attempt_id: AttemptId
    brief_artifact_ref_id: ArtifactRefId
    definition_revision: int
    definition_digest: str


@dataclass(frozen=True, slots=True)
class DependencyAcceptancePin:
    """The target has a committed checkpoint acceptance on some attempt of its producer."""

    target: work_models.CheckpointDependency


@dataclass(frozen=True, slots=True)
class DependencyFinishedProducerPin:
    """The target's producer is done or superseded, which satisfies every checkpoint of it."""

    target: work_models.CheckpointDependency


type DependencyCheckpointPin = (
    DependencyDefinitionPin | DependencyBriefPin | DependencyAcceptancePin | DependencyFinishedProducerPin
)


def new_checkpoint_dependencies(
    snapshot: LedgerSnapshot, dependent: WorkItemId | None, dependencies: tuple[work_models.Dependency, ...]
) -> tuple[work_models.CheckpointDependency, ...]:
    """Checkpoint dependencies that the dependent's current definition does not already hold."""

    current = None if dependent is None else snapshot.definition(dependent)
    unchanged = () if current is None else current.definition.dependencies
    return tuple(
        value
        for value in dependencies
        if isinstance(value, work_models.CheckpointDependency) and value not in unchanged
    )


def validate_dependency_brief_pins(
    snapshot: LedgerSnapshot,
    dependent: WorkItemId | None,
    dependencies: tuple[work_models.Dependency, ...],
    pins: tuple[DependencyCheckpointPin, ...],
) -> DecisionFailure | None:
    requested = new_checkpoint_dependencies(snapshot, dependent, dependencies)
    if tuple(pin.target for pin in pins) != requested:
        targets = ", ".join(f"'{value.item_id}' checkpoint '{value.checkpoint_id}'" for value in requested) or "none"
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"New checkpoint dependencies ({targets}) require verified targets from each prerequisite's current definition, accepted brief, committed checkpoint acceptance or finished state; inspect the item and those prerequisites before authoring them.",
            None,
        )
    for pin in pins:
        item = snapshot.work_item(pin.target.item_id)
        definition = snapshot.definition(pin.target.item_id)
        match pin:
            case DependencyDefinitionPin():
                if (
                    item is None
                    or definition is None
                    or (definition.revision, definition.digest) != (pin.definition_revision, pin.definition_digest)
                    or pin.target.checkpoint_id not in definition.definition.checkpoints
                ):
                    return DecisionFailure(
                        DecisionFailureCode.ITEM_DEFINITION_STALE,
                        f"Prerequisite '{pin.target.item_id}' changed its definition while checkpoint '{pin.target.checkpoint_id}' was selected. Inspect that prerequisite and select its current declared checkpoint again.",
                        None,
                    )
            case DependencyBriefPin():
                attempt = snapshot.attempt(pin.attempt_id)
                if (
                    item is None
                    or item.attempt != pin.attempt_id
                    or attempt is None
                    or attempt.brief_artifact_ref_id != pin.brief_artifact_ref_id
                    or definition is None
                    or (definition.revision, definition.digest) != (pin.definition_revision, pin.definition_digest)
                    or (attempt.accepted_scope_revision, attempt.accepted_scope_digest)
                    != (pin.definition_revision, pin.definition_digest)
                ):
                    return DecisionFailure(
                        DecisionFailureCode.ITEM_DEFINITION_STALE,
                        f"Prerequisite '{pin.target.item_id}' changed its accepted brief or definition while checkpoint '{pin.target.checkpoint_id}' was selected. Inspect that prerequisite and select its current declared checkpoint again.",
                        None,
                    )
            case DependencyAcceptancePin():
                if pin.target not in snapshot.accepted_checkpoints:
                    return DecisionFailure(
                        DecisionFailureCode.ITEM_DEFINITION_STALE,
                        f"Prerequisite '{pin.target.item_id}' no longer shows a committed acceptance of checkpoint '{pin.target.checkpoint_id}'. Inspect that prerequisite and select its checkpoint again.",
                        None,
                    )
            case DependencyFinishedProducerPin():
                if pin.target.item_id not in snapshot.history_items or pin.target.item_id in snapshot.dropped_item_ids:
                    return DecisionFailure(
                        DecisionFailureCode.ITEM_DEFINITION_STALE,
                        f"Prerequisite '{pin.target.item_id}' is no longer done or superseded while checkpoint '{pin.target.checkpoint_id}' was selected. Inspect that prerequisite and select its checkpoint again.",
                        None,
                    )
            case _ as unreachable:
                assert_never(unreachable)
    return None
