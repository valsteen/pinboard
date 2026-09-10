"""Project read-only application views from exact capabilities or stored snapshots.

Callers own SQLite access and time sampling. Exact status use cases request only
their operation facts; remaining projections select from an already-loaded complete
snapshot. These functions never read files, mutate state, or present output.
"""

from dataclasses import replace
from datetime import datetime
from typing import assert_never

from pinboard.application import ports, query_models, stored_state
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import ActionCapabilityFactory, project_attempt_action_groups
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, CandidateId, HistoryId, ItemId, TaskId
from pinboard.domain.ledger import LedgerSnapshot


def select_attempt_authority_status(
    reader: ports.AuthorityStatusReader, attempt_id: AttemptId
) -> DecisionResult[query_models.AttemptAuthorityStatus]:
    selected = reader.read_attempt_authority_status(attempt_id)
    if selected is None:
        return DecisionFailure(
            DecisionFailureCode.ATTEMPT_LEASE_REQUIRED,
            f"Attempt '{attempt_id}' has no retained authority.",
            None,
        )
    return selected


def select_preparation_authority_status(
    reader: ports.AuthorityStatusReader, item_id: ItemId, observed_at: datetime
) -> DecisionResult[query_models.PreparationAuthorityStatus]:
    selected = reader.read_preparation_authority_status(item_id)
    if selected is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Item '{item_id}' has no preparation claim.",
            None,
        )
    if selected.status == authority_models.PreparationLeaseStatus.ACTIVE and selected.expires_at <= observed_at:
        return replace(selected, status=authority_models.PreparationLeaseStatus.EXPIRED)
    return selected


def select_attempt_context(
    reader: ports.AttemptContextReader,
    attempt_id: AttemptId,
) -> DecisionResult[query_models.AttemptContextFacts]:
    selected = reader.read_attempt_context(attempt_id)
    if selected is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Attempt '{attempt_id}' does not exist.",
            None,
        )
    return selected


def select_review_job_context(
    reader: ports.ReviewJobContextReader,
    attempt_id: AttemptId,
    checkpoint_history_id: HistoryId | None,
    correction_history_id: HistoryId | None,
) -> DecisionResult[query_models.ReviewJobContextFacts]:
    selected = reader.read_review_job_context(attempt_id, checkpoint_history_id, correction_history_id)
    if selected is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Attempt '{attempt_id}' does not exist.",
            None,
        )
    return selected


def project_attempt_continuation(
    context: query_models.AttemptContextFacts,
    owner_task_id: TaskId | None,
) -> DecisionResult[query_models.AttemptContinuation]:
    """Select a continuation from one exact named-attempt context.

    A lifecycle state does not prove that a human decision is missing. Recorded
    pause conditions and accepted scope still determine whether the task must ask.
    The caller resolves the owner from the verified accepted brief.
    """
    match context:
        case query_models.TerminalAttemptContextFacts():
            return query_models.AttemptContinuation(
                "pinboard-attempt-continuation/v1",
                context.attempt_id,
                context.item_id,
                context.project_revision,
                work_models.AttemptState.DONE,
                None,
                True,
                False,
                None,
                (),
                ("create-user-task", "wake-user-task", "return-ownership-to-parent"),
            )
        case query_models.NonterminalAttemptContextFacts():
            item = context.item
            attempt_record = work_models.AttemptRecord(
                context.attempt_id,
                context.item_id,
                context.state,
                context.accepted_scope_revision,
                context.accepted_scope_digest,
                None if context.candidate_revision is None else CandidateId(context.candidate_revision),
                context.brief_artifact_ref_id,
            )
            groups = project_attempt_action_groups(
                work_models.ProjectAttemptActionContext(
                    item.item_id,
                    item.subject_revision,
                    work_models.WorkState(item.state.value),
                    context.attempt_id,
                    context.subject_revision,
                    attempt_record,
                    item.current_definition_revision,
                    item.current_definition_digest,
                    item.live_dependencies,
                    True,
                ),
                ActionCapabilityFactory(
                    decision_models.ActorAuthority(
                        decision_models.Role.PROJECT,
                        decision_models.AuthorizationKind.PROJECT,
                        0,
                    ),
                ),
            )
            actions = (*groups.attempt_actions, *groups.item_actions)
            selected = _next_attempt_operation(context, actions)
            if isinstance(selected, DecisionFailure):
                return selected
            return query_models.AttemptContinuation(
                "pinboard-attempt-continuation/v1",
                context.attempt_id,
                context.item_id,
                context.project_revision,
                context.state,
                owner_task_id,
                False,
                False,
                selected,
                tuple(decision_models.action_id(value) for value in actions),
                ("create-user-task", "wake-user-task", "return-ownership-to-parent"),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _next_attempt_operation(
    context: query_models.NonterminalAttemptContextFacts,
    actions: tuple[decision_models.Action, ...],
) -> DecisionResult[
    query_models.ActionContinuation | query_models.ReviewContinuation | query_models.DependencyContinuation
]:
    for action in actions:
        if isinstance(action, decision_models.AcceptCheckpointAction):
            if context.candidate_revision is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE, "Review has no protected candidate.", None
                )
            return query_models.ReviewContinuation(
                context.attempt_id,
                context.candidate_revision,
                "runtime-subagent",
            )
        if isinstance(action, decision_models.ContinueAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action), action.kind, "Follow the accepted brief."
            )
    for action in actions:
        if isinstance(action, decision_models.ReturnForCorrectionAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action),
                action.kind,
                "Clear the stale protected candidate, then bind the revised accepted brief.",
            )
        if isinstance(action, decision_models.PauseAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action),
                action.kind,
                "Preserve the attempt before binding a revised accepted brief.",
            )
        if isinstance(action, decision_models.ResumeAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action),
                action.kind,
                "Resolve the recorded pause or dependency condition and provide the matching current accepted brief.",
            )
    if context.item.live_dependencies:
        return query_models.DependencyContinuation(tuple(str(value) for value in context.item.live_dependencies))
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        f"Attempt '{context.attempt_id}' has no supported continuation among its current legal actions.",
        None,
    )


def _dependency_key(value: stored_state.ItemDependency) -> tuple[int, str]:
    return value.position, str(value.dependency_id)


def _dependency_position(value: stored_state.ItemDependency) -> int:
    return value.position


def _item_key(value: stored_state.StoredWorkItem) -> tuple[int, str]:
    return value.queue_position if value.queue_position is not None else 0, str(value.item_id)


def _decision_item_key(value: work_models.WorkItem) -> tuple[int, str]:
    return value.queue_position if value.queue_position is not None else 0, str(value.item)


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


def _project_selected_preparation_status(
    selected: query_models.PreparationAuthorityStatus | None,
    now: datetime,
) -> query_models.PreparationStatusView | None:
    if selected is None:
        return None
    status = (
        authority_models.PreparationLeaseStatus.EXPIRED
        if selected.status == authority_models.PreparationLeaseStatus.ACTIVE and selected.expires_at <= now
        else selected.status
    )
    return query_models.PreparationStatusView(
        selected.definition_revision,
        selected.definition_digest,
        str(selected.task_id),
        str(selected.host_id),
        str(selected.lease_id),
        selected.generation,
        selected.expires_at.isoformat(),
        status,
    )


def _proposal_maps(
    proposals: tuple[stored_state.StoredProposal, ...],
) -> tuple[
    dict[ItemId, stored_state.StoredProposal],
    dict[tuple[ItemId, ItemId], stored_state.StoredProposal],
]:
    by_item = {ItemId(proposal.proposal_id): proposal for proposal in proposals}
    prerequisites = {
        (proposal.relation.item, ItemId(proposal.proposal_id)): proposal
        for proposal in proposals
        if isinstance(proposal.relation, work_models.PrerequisiteProposalRelation)
    }
    return by_item, prerequisites


def _dependency_reason(
    proposals: dict[ItemId, stored_state.StoredProposal],
    prerequisite_proposals: dict[tuple[ItemId, ItemId], stored_state.StoredProposal],
    item_id: ItemId,
    dependency_id: ItemId,
) -> query_models.DependencyReason:
    proposal = proposals.get(item_id)
    if (
        proposal is not None
        and isinstance(proposal.relation, work_models.FollowUpProposalRelation)
        and proposal.relation.item == dependency_id
    ):
        reason = f"Follow-up to {dependency_id}: {proposal.why_it_matters}"
    else:
        prerequisite = prerequisite_proposals.get((item_id, dependency_id))
        reason = (
            f"Inferred prerequisite {dependency_id}: {prerequisite.why_it_matters}"
            if prerequisite is not None
            else "Recorded dependency."
        )
    return query_models.DependencyReason(str(dependency_id), reason)


def _review_flags(
    proposals: dict[ItemId, stored_state.StoredProposal], item_id: ItemId
) -> tuple[query_models.ReviewFlag, ...]:
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
    if proposal.disposition is not None or proposal.relation.kind not in {
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
    proposals, prerequisite_proposals = _proposal_maps(state.proposals.proposals)
    preparation_anchors = {
        (anchor.item_id, anchor.generation): anchor for anchor in state.authority.preparation_generations
    }
    preparations = {
        lease.item_id: (lease, preparation_anchors.get((lease.item_id, lease.generation)))
        for lease in state.authority.preparation_leases
    }
    live_items = _select_live_items(state)
    live_ids = frozenset(item.item_id for item, _live_state in live_items)

    items = tuple(
        query_models.OverviewItem(
            str(item.item_id),
            definitions[item.item_id].title,
            live_state,
            item.queue_position,
            not any(link.dependency_id in live_ids for link in dependency_links[item.item_id]),
            item.timing.value if item.timing is not None else None,
            tuple(str(link.dependency_id) for link in dependency_links[item.item_id]),
            tuple(
                _dependency_reason(proposals, prerequisite_proposals, item.item_id, link.dependency_id)
                for link in dependency_links[item.item_id]
            ),
            _review_flags(proposals, item.item_id),
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
        "sqlite-v5",
        str(state.lifecycle.project.revision),
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
    )


def _project_overview_item(
    item: work_models.WorkItem,
    label: str,
    live_dependencies: frozenset[ItemId],
    proposals: dict[ItemId, stored_state.StoredProposal],
    prerequisite_proposals: dict[tuple[ItemId, ItemId], stored_state.StoredProposal],
    preparation: query_models.PreparationAuthorityStatus | None,
    now: datetime,
) -> query_models.OverviewItem:
    return query_models.OverviewItem(
        str(item.item),
        label,
        item.state,
        item.queue_position,
        not any(dependency in live_dependencies for dependency in item.depends_on),
        item.timing,
        tuple(str(value) for value in item.depends_on),
        tuple(_dependency_reason(proposals, prerequisite_proposals, item.item, value) for value in item.depends_on),
        _review_flags(proposals, item.item),
        None if item.attempt is None else str(item.attempt),
        item.next_action,
        item.source,
        item.notes,
        _project_selected_preparation_status(preparation, now),
    )


def project_current_overview(facts: query_models.ProjectOverviewFacts, now: datetime) -> query_models.WorkOverview:
    """Project overview output from current facts that exclude retained history."""

    snapshot = facts.snapshot
    definitions = {value.item: value.definition for value in snapshot.definitions}
    live_ids = frozenset(item.item for item in snapshot.items)
    proposals, prerequisite_proposals = _proposal_maps(facts.proposals)
    preparations = {value.item_id: value for value in facts.preparations}

    items = tuple(
        _project_overview_item(
            item,
            definitions[item.item].title,
            live_ids,
            proposals,
            prerequisite_proposals,
            preparations.get(item.item),
            now,
        )
        for item in sorted(snapshot.items, key=_decision_item_key)
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (item.preparation is None or item.preparation.status != authority_models.PreparationLeaseStatus.ACTIVE)
        and item.state
        in {
            work_models.WorkState.INTAKE,
            work_models.WorkState.READY,
            work_models.WorkState.DEFERRED,
            work_models.WorkState.PAUSED,
            work_models.WorkState.BLOCKED,
        }
    )
    return query_models.WorkOverview(
        "pinboard-overview/v3",
        "sqlite-v5",
        snapshot.revision,
        tuple(
            str(attempt.attempt) for attempt in snapshot.attempts if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
    )


def project_item_overview(facts: query_models.ItemOverviewFacts, now: datetime) -> query_models.OverviewItem:
    """Project one live item from its exact view relationships."""

    item = facts.item
    proposals, prerequisite_proposals = _proposal_maps(facts.proposals)
    live_dependencies = frozenset(dependency_id for dependency_id, is_live in facts.dependency_liveness if is_live)
    return _project_overview_item(
        item,
        facts.definition.definition.title,
        live_dependencies,
        proposals,
        prerequisite_proposals,
        facts.preparation,
        now,
    )


def project_item_status(
    reader: ports.ItemStatusReader,
    item_id: ItemId,
    now: datetime,
) -> DecisionResult[query_models.ItemStatus]:
    facts = reader.read_item_status(item_id)
    if facts is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' was not found.", None)
    item = facts.item
    if facts.definition_title is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Item '{item_id}' has no definition.", None
        )
    attempts = tuple(
        query_models.ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in facts.attempts
    )
    return query_models.ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v5",
        str(facts.project_revision),
        str(item.item_id),
        facts.definition_title,
        item.state,
        item.timing,
        item.outcome_evidence,
        item.next_action,
        item.source,
        item.notes,
        item.queue_position,
        attempts,
        _project_selected_preparation_status(facts.preparation, now),
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


def select_item_definition(
    reader: ports.ItemDefinitionReader, item_id: ItemId
) -> DecisionResult[query_models.ItemDefinition]:
    selected = reader.read_item_definition(item_id)
    if selected.item_subject_revision is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.", None)
    if selected.definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Item '{item_id}' has no accepted definition.",
            None,
        )
    return query_models.ItemDefinition(
        "pinboard-item-definition/v1",
        "sqlite-v5",
        selected.project_revision,
        item_id,
        selected.item_subject_revision,
        selected.definition.revision,
        selected.definition.digest,
        _project_definition(selected.definition.definition),
    )


def select_item_definition_history(
    reader: ports.ItemDefinitionReader,
    item_id: ItemId,
    *,
    limit: int,
    before_revision: int | None,
) -> DecisionResult[query_models.ItemDefinitionHistory]:
    selected = reader.read_item_definition_history(item_id, limit=limit, before_revision=before_revision)
    if not selected.item_exists:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{item_id}' does not exist.", None)
    visible = selected.revisions[:limit]
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
        for value in visible
    )
    return query_models.ItemDefinitionHistory(
        "pinboard-item-definition-history/v1",
        "sqlite-v5",
        selected.project_revision,
        item_id,
        rows,
        rows[-1].revision if len(selected.revisions) > limit else None,
    )


def _classify_parallel_exclusion_reasons(
    item: query_models.ParallelPreviewItemFacts,
    operation_time: datetime,
) -> tuple[query_models.ParallelReason, ...]:
    item_id = item.item_id
    if item.state not in {work_models.WorkState.READY, work_models.WorkState.ACTIVE}:
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
            ),
        )
    if (
        item.preparation is not None
        and item.preparation.status == authority_models.PreparationLeaseStatus.ACTIVE
        and item.preparation.expires_at > operation_time
    ):
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.PREPARATION_OWNED,
                f"Item '{item_id}' is being prepared until {item.preparation.expires_at.isoformat()}.",
            ),
        )
    if item.live_dependencies:
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.DEPENDENCY_LIVE,
                f"Item '{item_id}' still depends on live work: {', '.join(item.live_dependencies)}.",
            ),
        )
    if (
        item.attempt is not None
        and item.attempt.state == work_models.AttemptState.ACTIVE
        and item.attempt.authority_status == authority_models.AttemptLeaseStatus.ACTIVE
        and item.attempt.authority_expires_at is not None
        and operation_time < item.attempt.authority_expires_at
    ):
        return (
            query_models.ParallelReason(
                query_models.ParallelReasonCode.ATTEMPT_OWNED,
                f"Active attempt '{item.attempt.attempt_id}' is owned until "
                f"{item.attempt.authority_expires_at.isoformat()}.",
            ),
        )
    return ()


def _complete_parallel_preview_facts(state: stored_state.StoredWorkState) -> query_models.ParallelPreviewFacts:
    live = _select_live_items(state)
    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
    live_ids = frozenset(item.item_id for item, _live_state in live)
    preparations_by_item = {lease.item_id: lease for lease in state.authority.preparation_leases}
    open_attempts_by_item: dict[
        ItemId,
        tuple[stored_state.StoredAttempt, query_models.NonterminalAttemptState],
    ] = {}
    for stored_attempt in state.lifecycle.attempts:
        match stored_attempt.state:
            case work_models.AttemptState.DONE:
                continue
            case (
                work_models.AttemptState.ACTIVE
                | work_models.AttemptState.PAUSED
                | work_models.AttemptState.BLOCKED
                | work_models.AttemptState.REVIEW
            ) as attempt_state:
                open_attempts_by_item[stored_attempt.item_id] = stored_attempt, attempt_state
            case _ as unreachable:
                assert_never(unreachable)
    attempt_leases_by_attempt = {lease.attempt_id: lease for lease in state.authority.attempt_leases}
    live_dependency_groups: dict[ItemId, list[ItemId]] = {item.item_id: [] for item, _live_state in live}
    for link in sorted(state.lifecycle.dependencies, key=_dependency_position):
        if link.dependency_id in live_ids:
            live_dependency_groups[link.item_id].append(link.dependency_id)
    items: list[query_models.ParallelPreviewItemFacts] = []
    for item, live_state in live:
        preparation_lease = preparations_by_item.get(item.item_id)
        preparation = (
            None
            if preparation_lease is None
            else query_models.ParallelPreparationFacts(preparation_lease.state, preparation_lease.expires_at)
        )
        stored_attempt_context = open_attempts_by_item.get(item.item_id)
        attempt = None
        if stored_attempt_context is not None:
            stored_attempt, attempt_state = stored_attempt_context
            attempt_lease = (
                attempt_leases_by_attempt.get(stored_attempt.attempt_id)
                if attempt_state == work_models.AttemptState.ACTIVE
                else None
            )
            attempt = query_models.ParallelAttemptFacts(
                stored_attempt.attempt_id,
                attempt_state,
                None if attempt_lease is None else attempt_lease.state,
                None if attempt_lease is None else attempt_lease.expires_at,
            )
        items.append(
            query_models.ParallelPreviewItemFacts(
                item.item_id,
                definitions[item.item_id].title,
                live_state,
                tuple(live_dependency_groups[item.item_id]),
                preparation,
                attempt,
            )
        )
    return query_models.ParallelPreviewFacts(state.lifecycle.project.revision, tuple(items))


def _project_parallel_preview_facts(
    facts: query_models.ParallelPreviewFacts,
    selection: query_models.ParallelSelection,
    now: datetime,
) -> query_models.ParallelPreview:
    items: list[query_models.ParallelItem] = []
    for item in facts.items:
        reasons = _classify_parallel_exclusion_reasons(item, now)
        common = (
            str(item.item_id),
            item.label,
            item.state,
            None if item.attempt is None else str(item.attempt.attempt_id),
        )
        items.append(
            query_models.ExcludedParallelItem(*common, reasons)
            if reasons
            else query_models.LaunchableParallelItem(*common)
        )
    return query_models.ParallelPreview(
        "pinboard-parallel-preview/v1",
        str(facts.project_revision),
        selection,
        selection == query_models.ParallelSelection.ALL_SAFE
        or not any(isinstance(item, query_models.ExcludedParallelItem) for item in items),
        tuple(sorted(items, key=_parallel_item_key)),
    )


def project_parallel_preview(state: stored_state.StoredWorkState, *, now: datetime) -> query_models.ParallelPreview:
    return _project_parallel_preview_facts(
        _complete_parallel_preview_facts(state),
        query_models.ParallelSelection.ALL_SAFE,
        now,
    )


def project_current_parallel_preview(snapshot: LedgerSnapshot, *, now: datetime) -> query_models.ParallelPreview:
    """Project all-safe parallel work from current decision facts only."""

    definitions = {value.item: value.definition for value in snapshot.definitions}
    live_ids = frozenset(item.item for item in snapshot.items)
    attempts = {value.attempt: value for value in snapshot.attempts}
    attempt_authorities = {value.attempt: value for value in snapshot.command_attempt_authorities}
    preparations = {value.item: value for value in snapshot.command_preparation_authorities}
    items: list[query_models.ParallelPreviewItemFacts] = []
    for item in snapshot.items:
        command_preparation = preparations.get(item.item)
        preparation = (
            None
            if command_preparation is None
            else query_models.ParallelPreparationFacts(
                authority_models.PreparationLeaseStatus.ACTIVE,
                command_preparation.expires_at,
            )
        )
        stored_attempt = None if item.attempt is None else attempts.get(item.attempt)
        attempt = None
        if stored_attempt is not None and stored_attempt.state != work_models.AttemptState.DONE:
            command_authority = attempt_authorities.get(stored_attempt.attempt)
            attempt = query_models.ParallelAttemptFacts(
                stored_attempt.attempt,
                stored_attempt.state,
                None if command_authority is None else authority_models.AttemptLeaseStatus.ACTIVE,
                None if command_authority is None else command_authority.expires_at,
            )
        items.append(
            query_models.ParallelPreviewItemFacts(
                item.item,
                definitions[item.item].title,
                item.state,
                tuple(dependency for dependency in item.depends_on if dependency in live_ids),
                preparation,
                attempt,
            )
        )
    return _project_parallel_preview_facts(
        query_models.ParallelPreviewFacts(int(snapshot.revision), tuple(items)),
        query_models.ParallelSelection.ALL_SAFE,
        now,
    )


def select_parallel_preview(
    reader: ports.ParallelPreviewReader,
    *,
    selected: tuple[str, ...],
    now: datetime,
) -> query_models.ParallelPreview | query_models.ParallelSelectionInvalid:
    facts = reader.read_parallel_preview(tuple(ItemId(item_id) for item_id in selected))
    if facts is None:
        return query_models.ParallelSelectionInvalid("Selected item identities must be current items.")
    return _project_parallel_preview_facts(facts, query_models.ParallelSelection.SELECTED, now)
