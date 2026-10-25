"""Recheck verified checkpoint declarations against locked producer facts."""

from dataclasses import dataclass

from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import ArtifactRefId, AttemptId
from pinboard.domain.ledger import LedgerSnapshot


@dataclass(frozen=True, slots=True)
class DependencyBriefPin:
    target: work_models.CheckpointDependency
    attempt_id: AttemptId
    brief_artifact_ref_id: ArtifactRefId
    definition_revision: int
    definition_digest: str


def validate_dependency_brief_pins(
    snapshot: LedgerSnapshot,
    dependencies: tuple[work_models.Dependency, ...],
    pins: tuple[DependencyBriefPin, ...],
) -> DecisionFailure | None:
    requested = tuple(value for value in dependencies if isinstance(value, work_models.CheckpointDependency))
    if tuple(pin.target for pin in pins) != requested:
        targets = ", ".join(f"'{value.item_id}' checkpoint '{value.checkpoint_id}'" for value in requested)
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Checkpoint dependencies ({targets}) require verified declarations from each prerequisite's current accepted brief; inspect those prerequisites before authoring them.",
            None,
        )
    for pin in pins:
        item = snapshot.work_item(pin.target.item_id)
        attempt = snapshot.attempt(pin.attempt_id)
        definition = snapshot.definition(pin.target.item_id)
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
    return None
