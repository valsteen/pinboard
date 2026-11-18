"""Verify new checkpoint targets, including selected immutable brief bytes, before dependency authoring.

A target is accepted through the producer's current declared checkpoints, its current accepted brief, a committed
acceptance of that checkpoint on any attempt, or a done or superseded producer. A revision resolves only targets its
dependent's current definition does not already hold. The application rechecks each pin under its write lock. This
owner reads no unrelated history and performs no publication or lifecycle effects.
"""

from collections.abc import Mapping
from datetime import datetime

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.application import ports, query_models, stored_state, work_brief_models
from pinboard.application.dependency_authoring import (
    DependencyAcceptancePin,
    DependencyBriefPin,
    DependencyCheckpointPin,
    DependencyDefinitionPin,
    DependencyFinishedProducerPin,
    new_checkpoint_dependencies,
)
from pinboard.application.work_briefs import decode_canonical_accepted_brief
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


def resolve_dependency_brief_pins(
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    dependent: WorkItemId | None,
    dependencies: tuple[work_models.Dependency, ...],
    now: datetime,
) -> DecisionResult[tuple[DependencyCheckpointPin, ...]]:
    requested = tuple(value for value in dependencies if isinstance(value, work_models.CheckpointDependency))
    if not requested:
        return ()
    facts = store.read_decision_facts(
        query_models.DecisionScope(
            tuple(
                dict.fromkeys((*(value.item_id for value in requested), *(() if dependent is None else (dependent,))))
            ),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            requested,
        ),
        now,
    )
    snapshot = facts.snapshot
    references = store.read_artifact_references_by_ids(
        tuple(
            attempt.brief_artifact_ref_id for attempt in snapshot.attempts if attempt.brief_artifact_ref_id is not None
        )
    )
    lineages = {value.attempt_id: value for value in facts.attempt_lineage}
    pins: list[DependencyCheckpointPin] = []
    for target in new_checkpoint_dependencies(snapshot, dependent, dependencies):
        declared = _declared_pin(snapshot, artifacts, references, lineages, target)
        if not isinstance(declared, DecisionFailure):
            pins.append(declared)
        elif target in snapshot.accepted_checkpoints:
            pins.append(DependencyAcceptancePin(target))
        elif target.item_id in snapshot.history_items and target.item_id not in snapshot.dropped_item_ids:
            pins.append(DependencyFinishedProducerPin(target))
        else:
            return declared
    return tuple(pins)


def _declared_pin(
    snapshot: LedgerSnapshot,
    artifacts: ArtifactRepository,
    references: Mapping[ArtifactRefId, stored_state.ArtifactReference],
    lineages: Mapping[AttemptId, query_models.AttemptLineage],
    target: work_models.CheckpointDependency,
) -> DecisionResult[DependencyCheckpointPin]:
    """Pin a target the live producer declares in its current definition or current accepted brief."""

    item = snapshot.work_item(target.item_id)
    definition = snapshot.definition(target.item_id)
    if item is not None and definition is not None and target.checkpoint_id in definition.definition.checkpoints:
        return DependencyDefinitionPin(target, definition.revision, definition.digest)
    attempt = None if item is None or item.attempt is None else snapshot.attempt(item.attempt)
    reference = (
        None
        if attempt is None or attempt.brief_artifact_ref_id is None
        else references.get(attempt.brief_artifact_ref_id)
    )
    if attempt is None or reference is None or definition is None:
        if item is not None and definition is not None and definition.definition.checkpoints:
            known = ", ".join(str(value) for value in definition.definition.checkpoints)
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_INVALID,
                f"Prerequisite '{target.item_id}' does not declare checkpoint '{target.checkpoint_id}'. Known current checkpoint IDs: {known}. Inspect that prerequisite's current definition or accepted brief and use a declared ID.",
                None,
            )
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Prerequisite '{target.item_id}' has no current accepted brief declaring checkpoint '{target.checkpoint_id}'. Inspect that prerequisite's item status and accepted brief before choosing a checkpoint dependency.",
            None,
        )
    brief = decode_canonical_accepted_brief(artifacts.read(reference))
    lineage = lineages[attempt.attempt]
    if isinstance(brief, work_brief_models.WorkBriefFailure) or (
        brief.item_id,
        brief.attempt_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    ) != (
        target.item_id,
        attempt.attempt,
        lineage.branch,
        lineage.base_revision,
        definition.revision,
        definition.digest,
    ):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_STALE,
            f"Prerequisite '{target.item_id}' has an invalid or stale current accepted brief. Inspect and reconcile that prerequisite before authoring checkpoint '{target.checkpoint_id}'.",
            None,
        )
    if isinstance(brief, work_brief_models.SmallWorkBrief):
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Prerequisite '{target.item_id}' is a small item delivered inline under one implicit terminal checkpoint, so it offers no checkpoint dependency target. Depend on the item '{target.item_id}' itself instead of checkpoint '{target.checkpoint_id}'.",
            None,
        )
    declared = brief.checkpoint.checkpoint_id
    if target.checkpoint_id != declared:
        known_ids = (
            *(str(value) for value in definition.definition.checkpoints),
            declared,
        )
        known = ", ".join(dict.fromkeys(known_ids))
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Prerequisite '{target.item_id}' does not declare checkpoint '{target.checkpoint_id}'. Known current checkpoint IDs: {known}. Inspect that prerequisite's current accepted brief and use its declared ID.",
            None,
        )
    return DependencyBriefPin(
        target, attempt.attempt, reference.artifact_ref_id, definition.revision, definition.digest
    )
