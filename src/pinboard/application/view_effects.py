"""Select and retain only generated-view facts changed by one mutation."""

from dataclasses import dataclass
from datetime import datetime

from pinboard.application import queries, query_models, stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.identifiers import AttemptId, ItemId


@dataclass(frozen=True, slots=True)
class AffectedRecordIds:
    current_focus: bool
    items: tuple[ItemId, ...]
    attempts: tuple[AttemptId, ...]


@dataclass(frozen=True, slots=True)
class _ItemFacts:
    item: stored_state.StoredWorkItem
    eligible: bool
    preparation_status: authority_models.PreparationLeaseStatus | None
    dependencies: tuple[ItemId, ...]
    dependency_reasons: tuple[query_models.DependencyReason, ...]
    review_flags: tuple[query_models.ReviewFlag, ...]
    definition: stored_state.ItemDefinitionRevision


def _dependency_order(value: stored_state.ItemDependency) -> tuple[str, int]:
    return str(value.item_id), value.position


def _item_facts(state: stored_state.StoredWorkState, now: datetime) -> dict[ItemId, _ItemFacts]:
    overview = queries.project_overview(state, now)
    overview_items = {ItemId(value.item_id): value for value in overview.items}
    definitions = {value.item_id: value for value in state.lifecycle.definition_revisions}
    dependency_groups: dict[ItemId, list[ItemId]] = {value.item_id: [] for value in state.lifecycle.work_items}
    for value in sorted(state.lifecycle.dependencies, key=_dependency_order):
        dependency_groups[value.item_id].append(value.dependency_id)
    result: dict[ItemId, _ItemFacts] = {}
    for item in state.lifecycle.work_items:
        if item.queue_position is None:
            result[item.item_id] = _ItemFacts(
                item,
                False,
                None,
                tuple(dependency_groups[item.item_id]),
                (),
                (),
                definitions[item.item_id],
            )
            continue
        projected = overview_items[item.item_id]
        result[item.item_id] = _ItemFacts(
            item,
            projected.eligible,
            None if projected.preparation is None else projected.preparation.status,
            tuple(dependency_groups[item.item_id]),
            projected.dependency_reasons,
            projected.review_flags,
            definitions[item.item_id],
        )
    return result


def affected_record_ids(
    before: stored_state.StoredWorkState,
    after: stored_state.StoredWorkState,
    *,
    now: datetime,
) -> AffectedRecordIds:
    before_items = _item_facts(before, now)
    after_items = _item_facts(after, now)
    items = tuple(
        item.item_id
        for item in after.lifecycle.work_items
        if before_items.get(item.item_id) != after_items[item.item_id]
    )

    def attempt_facts(value: stored_state.StoredAttempt) -> tuple[str | None, ...]:
        if value.state == work_models.AttemptState.DONE:
            return (
                "terminal",
                str(value.item_id),
                value.branch,
                value.base_revision,
                value.candidate_revision,
            )
        return ("live", str(value.brief_artifact_ref_id))

    before_attempts = {value.attempt_id: attempt_facts(value) for value in before.lifecycle.attempts}
    attempts = tuple(
        value.attempt_id
        for value in after.lifecycle.attempts
        if before_attempts.get(value.attempt_id) != attempt_facts(value)
    )
    return AffectedRecordIds(before.focus != after.focus, items, attempts)


def compact_view_state(
    state: stored_state.StoredWorkState,
    affected: AffectedRecordIds,
    context_item_ids: tuple[ItemId, ...] = (),
) -> stored_state.StoredWorkState:
    """Keep the transitive facts needed to render only affected projections."""

    rendered_items = frozenset((*affected.items, *context_item_ids))
    dependency_ids = frozenset(
        link.dependency_id for link in state.lifecycle.dependencies if link.item_id in rendered_items
    )
    context_items = rendered_items | dependency_ids
    attempts = tuple(
        value
        for value in state.lifecycle.attempts
        if value.attempt_id in affected.attempts or value.item_id in rendered_items
    )
    proposals = tuple(
        value
        for value in state.proposals.proposals
        if ItemId(value.proposal_id) in context_items
        or (value.relation.item is not None and value.relation.item in rendered_items)
    )
    proposal_ids = frozenset(value.proposal_id for value in proposals)
    authority = state.authority
    return stored_state.StoredWorkState(
        stored_state.LifecycleRecords(
            state.lifecycle.project,
            tuple(value for value in state.lifecycle.work_items if value.item_id in context_items),
            tuple(value for value in state.lifecycle.dependencies if value.item_id in rendered_items),
            attempts,
            tuple(value for value in state.lifecycle.definition_revisions if value.item_id in context_items),
        ),
        stored_state.ProposalRecords(
            proposals,
            tuple(value for value in state.proposals.evidence if value.proposal_id in proposal_ids),
            tuple(value for value in state.proposals.freshness if value.proposal_id in proposal_ids),
        ),
        tuple(
            value
            for value in state.artifact_references
            if any(attempt.brief_artifact_ref_id == value.artifact_ref_id for attempt in attempts)
        ),
        stored_state.AuthorityRecords(
            authority.coordination,
            tuple(value for value in authority.attempt_counters if value.attempt_id in affected.attempts),
            tuple(value for value in authority.attempt_generations if value.attempt_id in affected.attempts),
            tuple(value for value in authority.attempt_leases if value.attempt_id in affected.attempts),
            tuple(value for value in authority.preparation_counters if value.item_id in context_items),
            tuple(value for value in authority.preparation_generations if value.item_id in context_items),
            tuple(value for value in authority.preparation_leases if value.item_id in context_items),
        ),
        state.transition_receipts,
        state.focus,
    )
