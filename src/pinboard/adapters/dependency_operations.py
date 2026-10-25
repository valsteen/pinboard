"""Verify selected immutable brief bytes before dependency authoring.

The application rechecks these pins under its write lock. This owner reads no
unrelated history and performs no publication or lifecycle effects.
"""

from datetime import datetime

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.application import ports, query_models, work_brief_models
from pinboard.application.dependency_authoring import DependencyBriefPin
from pinboard.application.work_briefs import decode_canonical_work_brief
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult


def resolve_dependency_brief_pins(
    store: ports.WorkStore,
    artifacts: ArtifactRepository,
    dependencies: tuple[work_models.Dependency, ...],
    now: datetime,
) -> DecisionResult[tuple[DependencyBriefPin, ...]]:
    requested = tuple(value for value in dependencies if isinstance(value, work_models.CheckpointDependency))
    if not requested:
        return ()
    facts = store.read_decision_facts(
        query_models.DecisionScope(
            tuple(value.item_id for value in requested),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
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
    pins: list[DependencyBriefPin] = []
    for target in requested:
        item = snapshot.work_item(target.item_id)
        attempt = None if item is None or item.attempt is None else snapshot.attempt(item.attempt)
        reference = (
            None
            if attempt is None or attempt.brief_artifact_ref_id is None
            else references.get(attempt.brief_artifact_ref_id)
        )
        definition = snapshot.definition(target.item_id)
        if attempt is None or reference is None or definition is None:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_INVALID,
                f"Prerequisite '{target.item_id}' has no current accepted brief declaring checkpoint '{target.checkpoint_id}'. Inspect that prerequisite's item status and accepted brief before choosing a checkpoint dependency.",
                None,
            )
        brief = decode_canonical_work_brief(artifacts.read(reference))
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
        declared = brief.checkpoint.checkpoint_id
        if target.checkpoint_id != declared:
            return DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_INVALID,
                f"Prerequisite '{target.item_id}' does not declare checkpoint '{target.checkpoint_id}'. Known current checkpoint IDs: {declared}. Inspect that prerequisite's current accepted brief and use its declared ID.",
                None,
            )
        pins.append(
            DependencyBriefPin(
                target, attempt.attempt, reference.artifact_ref_id, definition.revision, definition.digest
            )
        )
    return tuple(pins)
