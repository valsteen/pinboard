"""Project read-only application views from one already-loaded stored snapshot.

Callers own SQLite access and time sampling. These functions select and compose
current facts without reading files, mutating state, or presenting output.
"""

from datetime import datetime

from pinboard.application import query_models, stored_state
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ItemId


def _dependency_key(value: stored_state.ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: stored_state.ItemDependency) -> int:
    return value.position


def _item_key(value: stored_state.StoredWorkItem) -> tuple[int, str]:
    return value.queue_position if value.queue_position is not None else 0, str(value.item_id)


def _select_live_items(
    state: stored_state.StoredWorkState,
) -> tuple[tuple[stored_state.StoredWorkItem, work_models.WorkState], ...]:
    return tuple(
        (item, live_state)
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if (live_state := stored_state.live_work_state(item.state)) is not None
    )


def _attempt_key(value: stored_state.StoredAttempt) -> str:
    return str(value.attempt_id)


def _parallel_item_key(value: query_models.ParallelItem) -> str:
    return value.item_id


def _project_preparation_status(
    retained: tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration | None] | None,
    now: datetime,
) -> query_models.PreparationStatusView | None:
    if retained is None:
        return None
    lease, anchor = retained
    if anchor is None:
        return None
    status = (
        authority_models.PreparationLeaseStatus.EXPIRED
        if lease.state == authority_models.PreparationLeaseStatus.ACTIVE and lease.expires_at <= now
        else lease.state
    )
    return query_models.PreparationStatusView(
        lease.definition_revision,
        lease.definition_digest,
        str(anchor.task_id),
        str(anchor.host_id),
        str(anchor.lease_id),
        lease.generation,
        lease.expires_at.isoformat(),
        status,
    )


def project_overview(state: stored_state.StoredWorkState, now: datetime) -> query_models.WorkOverview:
    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    dependency_groups: dict[ItemId, list[stored_state.ItemDependency]] = {
        item.item_id: [] for item in state.lifecycle.work_items
    }
    for link in sorted(state.lifecycle.dependencies, key=_dependency_key):
        dependency_groups[link.item_id].append(link)
    dependency_links = {item_id: tuple(links) for item_id, links in dependency_groups.items()}
    proposals = {ItemId(proposal.proposal_id): proposal for proposal in state.proposals.proposals}
    prerequisite_proposals = {
        (proposal.relation.item, ItemId(proposal.proposal_id)): proposal
        for proposal in state.proposals.proposals
        if isinstance(proposal.relation, work_models.PrerequisiteProposalRelation)
    }
    preparation_anchors = {
        (anchor.item_id, anchor.generation): anchor for anchor in state.authority.preparation_generations
    }
    preparations = {
        lease.item_id: (lease, preparation_anchors.get((lease.item_id, lease.generation)))
        for lease in state.authority.preparation_leases
    }
    live_items = _select_live_items(state)
    live_ids = frozenset(item.item_id for item, _live_state in live_items)

    def dependency_reason(item_id: ItemId, link: stored_state.ItemDependency) -> query_models.DependencyReason:
        proposal = proposals.get(item_id)
        if (
            proposal is not None
            and isinstance(proposal.relation, work_models.FollowUpProposalRelation)
            and proposal.relation.item == link.dependency_id
        ):
            reason = f"Follow-up to {link.dependency_id}: {proposal.why_it_matters}"
        else:
            prerequisite = prerequisite_proposals.get((item_id, link.dependency_id))
            reason = (
                f"Inferred prerequisite {link.dependency_id}: {prerequisite.why_it_matters}"
                if prerequisite is not None
                else "Recorded dependency."
            )
        return query_models.DependencyReason(str(link.dependency_id), reason)

    def review_flags(item_id: ItemId) -> tuple[query_models.ReviewFlag, ...]:
        proposal = proposals.get(item_id)
        if proposal is None:
            return ()
        if isinstance(proposal.disposition, work_models.ReturnedProposalDisposition):
            return (
                query_models.ReviewFlag(
                    work_models.ProposalRelationKind.CLARIFICATION,
                    str(proposal.relation.item) if proposal.relation.item is not None else None,
                    proposal.disposition.reason,
                ),
            )
        if proposal.disposition is not None:
            return ()
        if proposal.relation.kind not in {
            work_models.ProposalRelationKind.DUPLICATE,
            work_models.ProposalRelationKind.CONTRADICTION,
            work_models.ProposalRelationKind.CLARIFICATION,
        }:
            return ()
        return (
            query_models.ReviewFlag(
                proposal.relation.kind,
                str(proposal.relation.item) if proposal.relation.item is not None else None,
                proposal.why_it_matters,
            ),
        )

    items = tuple(
        query_models.OverviewItem(
            str(item.item_id),
            definitions[item.item_id].title,
            live_state,
            item.queue_position,
            not any(link.dependency_id in live_ids for link in dependency_links[item.item_id]),
            item.timing.value if item.timing is not None else None,
            tuple(str(link.dependency_id) for link in dependency_links[item.item_id]),
            tuple(dependency_reason(item.item_id, link) for link in dependency_links[item.item_id]),
            review_flags(item.item_id),
            str(attempts[item.item_id]) if item.item_id in attempts else None,
            item.next_action,
            item.source,
            item.notes,
            _project_preparation_status(preparations.get(item.item_id), now),
        )
        for item, live_state in live_items
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (item.preparation is None or item.preparation.status != authority_models.PreparationLeaseStatus.ACTIVE)
        and (
            item.state in {work_models.WorkState.INTAKE, work_models.WorkState.READY, work_models.WorkState.DEFERRED}
            or item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}
        )
    )
    return query_models.WorkOverview(
        "pinboard-overview/v3",
        "sqlite-v4",
        str(state.lifecycle.project.revision),
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
    )


def project_item_status(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    now: datetime,
) -> DecisionResult[query_models.ItemStatus]:
    item = next((candidate for candidate in state.lifecycle.work_items if candidate.item_id == item_id), None)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' was not found.")
    definition = next(
        (value.definition for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item_id),
        None,
    )
    if definition is None:
        return DecisionFailure(DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Item '{item_id}' has no definition.")
    attempts = tuple(
        query_models.ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in sorted(
            (candidate for candidate in state.lifecycle.attempts if candidate.item_id == item_id),
            key=_attempt_key,
        )
    )
    return query_models.ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v4",
        str(state.lifecycle.project.revision),
        str(item.item_id),
        definition.title,
        item.state,
        item.timing,
        item.outcome_evidence,
        item.next_action,
        item.source,
        item.notes,
        item.queue_position,
        attempts,
        _project_preparation_status(stored_state.retained_preparation(state, item_id), now),
    )


def _project_definition(definition: work_models.WorkItemDefinition) -> query_models.WorkItemDefinitionView:
    return query_models.WorkItemDefinitionView(
        "pinboard-work-item-definition/v1",
        definition.title,
        definition.objective,
        definition.hypothesis,
        definition.evidence,
        definition.scope,
        definition.non_scope,
        definition.acceptance_criteria,
        tuple(definition.dependencies),
        definition.effect,
        definition.unlock,
    )


def project_item_definition(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
) -> DecisionResult[query_models.ItemDefinition]:
    item = next((value for value in state.lifecycle.work_items if value.item_id == item_id), None)
    if item is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    current_definition = next(
        (value for value in reversed(state.lifecycle.definition_revisions) if value.item_id == item_id),
        None,
    )
    if current_definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Item '{item_id}' has no accepted definition.",
        )
    return query_models.ItemDefinition(
        "pinboard-item-definition/v1",
        "sqlite-v4",
        state.lifecycle.project.revision,
        item_id,
        item.subject_revision,
        current_definition.revision,
        current_definition.digest,
        _project_definition(current_definition.definition),
    )


def project_item_definition_history(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    *,
    limit: int = 20,
    before_revision: int | None = None,
) -> DecisionResult[query_models.ItemDefinitionHistory]:
    if not any(item.item_id == item_id for item in state.lifecycle.work_items):
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.")
    available = tuple(
        value
        for value in reversed(state.lifecycle.definition_revisions)
        if value.item_id == item_id and (before_revision is None or value.revision < before_revision)
    )
    selected = available[:limit]
    rows = tuple(
        query_models.ItemDefinitionHistoryRow(
            value.revision,
            value.digest,
            _project_definition(value.definition),
            value.reason,
            value.source_task_id,
            value.accepted_at.isoformat(),
            value.before_digest,
            value.after_digest,
            value.accepted_project_revision,
        )
        for value in selected
    )
    return query_models.ItemDefinitionHistory(
        "pinboard-item-definition-history/v1",
        "sqlite-v4",
        state.lifecycle.project.revision,
        item_id,
        rows,
        rows[-1].revision if len(available) > limit else None,
    )


def _classify_parallel_exclusion_reasons(
    item: stored_state.StoredWorkItem,
    preparation: stored_state.StoredPreparationLease | None,
    live_dependencies: tuple[str, ...],
    active_attempt: stored_state.StoredAttempt | None,
    attempt_lease: stored_state.StoredAttemptLease | None,
    operation_time: datetime,
) -> tuple[query_models.ParallelReason, ...]:
    item_id = item.item_id
    if item.state not in {stored_state.StoredWorkItemState.READY, stored_state.StoredWorkItemState.ACTIVE}:
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    if (
        preparation is not None
        and preparation.state == authority_models.PreparationLeaseStatus.ACTIVE
        and preparation.expires_at > operation_time
    ):
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.PREPARATION_OWNED,
                f"Item '{item_id}' is being prepared until {preparation.expires_at.isoformat()}.",
            ),
        )
    if live_dependencies:
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(live_dependencies)}.",
            ),
        )
    if (
        active_attempt is not None
        and attempt_lease is not None
        and attempt_lease.state == authority_models.AttemptLeaseStatus.ACTIVE
        and operation_time < attempt_lease.expires_at
    ):
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.ATTEMPT_OWNED,
                f"Active attempt '{active_attempt.attempt_id}' is owned until {attempt_lease.expires_at.isoformat()}.",
            ),
        )
    return ()


def project_parallel_preview(
    state: stored_state.StoredWorkState,
    *,
    selected: tuple[str, ...] = (),
    now: datetime,
) -> query_models.ParallelPreview | query_models.ParallelSelectionInvalid:
    live = _select_live_items(state)
    by_id = {str(item.item_id): (item, live_state) for item, live_state in live}
    if any(item_id not in by_id for item_id in selected):
        return query_models.ParallelSelectionInvalid("Selected item identities must be current items.")
    candidates = tuple(by_id[item_id] for item_id in selected) if selected else live
    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
    live_ids = frozenset(by_id)
    preparations_by_item = {lease.item_id: lease for lease in state.authority.preparation_leases}
    open_attempts_by_item = {
        attempt.item_id: attempt
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    active_attempts_by_item = {
        item_id: attempt
        for item_id, attempt in open_attempts_by_item.items()
        if attempt.state == work_models.AttemptState.ACTIVE
    }
    attempt_leases_by_attempt = {lease.attempt_id: lease for lease in state.authority.attempt_leases}
    live_dependency_groups: dict[ItemId, list[str]] = {item.item_id: [] for item, _live_state in live}
    for link in sorted(state.lifecycle.dependencies, key=_dependency_position):
        if str(link.dependency_id) in live_ids:
            live_dependency_groups[link.item_id].append(str(link.dependency_id))
    items: list[query_models.ParallelItem] = []
    for item, live_state in candidates:
        active_attempt = active_attempts_by_item.get(item.item_id)
        reasons = _classify_parallel_exclusion_reasons(
            item,
            preparations_by_item.get(item.item_id),
            tuple(live_dependency_groups[item.item_id]),
            active_attempt,
            None if active_attempt is None else attempt_leases_by_attempt.get(active_attempt.attempt_id),
            now,
        )
        open_attempt = open_attempts_by_item.get(item.item_id)
        common = (
            str(item.item_id),
            definitions[item.item_id].title,
            live_state,
            None if open_attempt is None else str(open_attempt.attempt_id),
        )
        items.append(
            query_models.ExcludedParallelItem(*common, reasons)
            if reasons
            else query_models.LaunchableParallelItem(*common)
        )
    return query_models.ParallelPreview(
        "pinboard-parallel-preview/v1",
        str(state.lifecycle.project.revision),
        query_models.ParallelSelection.SELECTED if selected else query_models.ParallelSelection.ALL_SAFE,
        not selected or not any(isinstance(item, query_models.ExcludedParallelItem) for item in items),
        tuple(sorted(items, key=_parallel_item_key)),
    )
