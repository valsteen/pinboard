"""Project read-only application views from exact capabilities or stored snapshots.

Callers own SQLite access and time sampling. Exact status use cases request only
their operation facts; remaining projections select from an already-loaded complete
snapshot. These functions never read files, mutate state, or present output.
"""

from datetime import datetime
from typing import assert_never

from pinboard.application import ports, query_models, released_v6_compatibility, stored_state, work_brief_models
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import ActionCapabilityFactory, project_attempt_action_groups
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId, CandidateId, HistoryId, ItemId, TaskId
from pinboard.domain.ledger import LedgerSnapshot


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
    result_sha256: str | None,
    review_sha256: str | None,
) -> DecisionResult[query_models.ReviewJobContextFacts]:
    selected = reader.read_review_job_context(
        attempt_id, checkpoint_history_id, correction_history_id, result_sha256, review_sha256
    )
    if selected is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Attempt '{attempt_id}' does not exist.",
            None,
        )
    return selected


def validate_attempt_brief_identity(
    context: query_models.NonterminalAttemptContextFacts,
    brief: work_brief_models.ReadableWorkBrief,
) -> DecisionFailure | None:
    """Require one decoded brief to be the exact accepted identity for an attempt."""

    if (
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    ) == (
        context.attempt_id,
        context.item_id,
        context.branch,
        context.base_revision,
        context.accepted_scope_revision,
        context.accepted_scope_digest,
    ):
        return None
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Accepted brief identity differs from the attempt.",
        None,
    )


def project_attempt_continuation(
    context: query_models.AttemptContextFacts,
    owner_task_id: TaskId | None,
    brief: work_brief_models.ReadableWorkBrief | None,
    reconciliation: query_models.AttemptReconciliation | None,
    candidate_lineage: query_models.CandidateLineage | None,
    ready_review: bool,
) -> DecisionResult[query_models.AttemptContinuation]:
    """Select a continuation from one exact named-attempt context.

    A lifecycle state does not prove that a human decision is missing. Recorded
    pause conditions and accepted scope still determine whether the task must ask.
    The caller resolves the owner from the verified accepted brief.
    """
    match context:
        case query_models.TerminalAttemptContextFacts():
            return query_models.TerminalAttemptContinuation(
                "pinboard-attempt-continuation/v1",
                context.attempt_id,
                context.item_id,
                context.project_revision,
                None,
                True,
                False,
                None,
                (),
                ("create-user-task", "wake-user-task", "return-ownership-to-parent"),
            )
        case query_models.NonterminalAttemptContextFacts():
            if owner_task_id is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "A nonterminal attempt requires its verified owner task identity.",
                    None,
                )
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
                    item.current_replacement_revision,
                    item.replacement_resolved,
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
            if brief is None:
                return DecisionFailure(
                    DecisionFailureCode.ACTION_NOT_AVAILABLE,
                    "A nonterminal attempt requires its verified accepted brief.",
                    None,
                )
            selected = _next_attempt_operation(context, actions, brief, reconciliation, candidate_lineage, ready_review)
            if isinstance(selected, DecisionFailure):
                return selected
            continuation_arguments = (
                "pinboard-attempt-continuation/v1",
                context.attempt_id,
                context.item_id,
                context.project_revision,
                owner_task_id,
                False,
                False,
                selected,
                tuple(decision_models.action_id(value) for value in actions),
                ("create-user-task", "wake-user-task", "return-ownership-to-parent"),
            )
            match context.state:
                case work_models.AttemptState.ACTIVE:
                    return query_models.ActiveAttemptContinuation(*continuation_arguments)
                case work_models.AttemptState.REVIEW:
                    return query_models.ReviewAttemptContinuation(*continuation_arguments)
                case work_models.AttemptState.PAUSED:
                    return query_models.PausedAttemptContinuation(*continuation_arguments)
                case work_models.AttemptState.BLOCKED:
                    return query_models.BlockedAttemptContinuation(*continuation_arguments)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)


def _select_repository_disposition(
    reconciliation: query_models.AttemptReconciliation,
    candidate_lineage: query_models.CandidateLineage | None,
    actions: tuple[decision_models.Action, ...],
) -> DecisionResult[query_models.ActionContinuation | query_models.RepositoryDispositionContinuation]:
    if candidate_lineage == query_models.CandidateLineage.COMMIT_CURRENT:
        return query_models.RepositoryDispositionContinuation(reconciliation.target_revision, reconciliation.relation)
    for action in actions:
        if isinstance(action, decision_models.ReturnForCorrectionAction):
            condition = (
                "The protected candidate no longer matches the checkout. Apply return-for-correction to the same "
                "attempt with this lineage mismatch as the reason, preserve its history_id, obtain correction-source "
                "review, then dispatch correction work that submits a current clean commit candidate; no user input "
                "is required."
                if candidate_lineage == query_models.CandidateLineage.DRIFTED
                else "Repository disposition requires a current clean commit candidate. Apply return-for-correction "
                "to the same attempt with this requirement as the reason, preserve its history_id, obtain "
                "correction-source review, then dispatch correction work that commits and resubmits the candidate; "
                "no user input is required."
            )
            return query_models.ActionContinuation(decision_models.action_id(action), action.kind, condition)
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Candidate lineage correction is not currently available.",
        None,
    )


def select_resumed_review_operation(
    reconciliation: query_models.AttemptReconciliation,
    *,
    attempt_id: str,
    candidate_revision: str,
    candidate_lineage: query_models.CandidateLineage | None,
    ready_review: bool,
    actions: tuple[decision_models.Action, ...],
) -> DecisionResult[
    query_models.ActionContinuation | query_models.ReviewContinuation | query_models.ReconciliationContinuation
]:
    """Select the sole remaining reviewed-candidate operation from caller-observed repository facts."""

    if not ready_review:
        return query_models.ReviewContinuation(attempt_id, candidate_revision, "runtime-subagent")
    relation = reconciliation.relation
    if relation == query_models.IntegrationRelation.TARGET_STALE:
        return query_models.RefreshTargetContinuation(reconciliation.target_revision)
    if relation in (query_models.IntegrationRelation.CANDIDATE_RESIDUAL, query_models.IntegrationRelation.DIVERGED):
        for action in actions:
            if isinstance(action, decision_models.ReturnForCorrectionAction):
                return query_models.ActionContinuation(
                    decision_models.action_id(action),
                    action.kind,
                    "Return the reviewed candidate for correction against the current integration target.",
                )
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "Reviewed candidate correction is not currently available.",
            None,
        )
    for observation in reconciliation.effects:
        if observation.status in (
            query_models.RuntimeEffectStatus.DENIED,
            query_models.RuntimeEffectStatus.UNKNOWN,
        ):
            return query_models.PermissionRecoveryContinuation(
                reconciliation.target_revision,
                observation.effect,
                observation.status,
            )
    if relation in (
        query_models.IntegrationRelation.CANDIDATE_PENDING_ON_ACCEPTED_BASE,
        query_models.IntegrationRelation.CANDIDATE_PENDING_ON_SQUASH_EQUIVALENT_BASE,
    ):
        return _select_repository_disposition(reconciliation, candidate_lineage, actions)
    if reconciliation.phase == query_models.RepositoryPhase.CLEANUP:
        return query_models.RepositoryCleanupContinuation(reconciliation.target_revision)
    for action in actions:
        if isinstance(action, decision_models.CompleteAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action),
                action.kind,
                "Complete after the integrated candidate and required repository effects are verified.",
            )
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Reviewed candidate completion is not currently available.",
        None,
    )


def _next_attempt_operation(  # noqa: C901, PLR0912 - closed lifecycle continuation selection
    context: query_models.NonterminalAttemptContextFacts,
    actions: tuple[decision_models.Action, ...],
    brief: work_brief_models.ReadableWorkBrief,
    reconciliation: query_models.AttemptReconciliation | None,
    candidate_lineage: query_models.CandidateLineage | None,
    ready_review: bool,
) -> DecisionResult[query_models.NonterminalContinuationOperation]:
    if not isinstance(brief, work_brief_models.WorkBrief):
        expected_kind = (
            decision_models.ActionKind.RETURN_FOR_CORRECTION
            if context.state == work_models.AttemptState.REVIEW
            else decision_models.ActionKind.REBIND_ATTEMPT
            if context.state == work_models.AttemptState.ACTIVE
            else decision_models.ActionKind.RESUME
        )
        recovery = (
            "Return the reviewed candidate for correction first. Then publish and independently review a matching "
            "pinboard-work-brief/v4, rebind the active attempt, dispatch, and submit a new candidate."
            if expected_kind == decision_models.ActionKind.RETURN_FOR_CORRECTION
            else "Publish and independently review a matching pinboard-work-brief/v4, then bind that accepted brief "
            "through this exact action before dispatch and candidate submission."
        )
        for action in actions:
            if action.kind == expected_kind:
                return query_models.ActionContinuation(
                    decision_models.action_id(action),
                    action.kind,
                    recovery,
                )
    if isinstance(brief, work_brief_models.WorkBrief) and context.state == work_models.AttemptState.REVIEW:
        if context.candidate_revision is None:
            return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Review has no protected candidate.", None)
        if reconciliation is not None:
            return select_resumed_review_operation(
                reconciliation,
                attempt_id=str(context.attempt_id),
                candidate_revision=context.candidate_revision,
                candidate_lineage=candidate_lineage,
                ready_review=ready_review,
                actions=actions,
            )
        if ready_review:
            return DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "A ready review requires current repository reconciliation.",
                None,
            )
    if isinstance(brief, work_brief_models.WorkBrief) and isinstance(
        brief.checkpoint.disposition, work_brief_models.TerminalCheckpointDisposition
    ):
        for action in actions:
            if isinstance(action, decision_models.CompleteAction):
                if context.state == work_models.AttemptState.REVIEW:
                    if context.candidate_revision is None:
                        return DecisionFailure(
                            DecisionFailureCode.ACTION_NOT_AVAILABLE, "Review has no protected candidate.", None
                        )
                    return query_models.ReviewContinuation(
                        context.attempt_id,
                        context.candidate_revision,
                        "runtime-subagent",
                    )
                return query_models.ActionContinuation(
                    decision_models.action_id(action),
                    action.kind,
                    "Discover the focused completion action and follow its candidate-submission recovery.",
                )
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
        if context.state == work_models.AttemptState.ACTIVE and isinstance(action, decision_models.RebindAttemptAction):
            return query_models.ActionContinuation(
                decision_models.action_id(action),
                action.kind,
                "Bind a current accepted v4 brief that matches the current definition.",
            )
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


def _proposal_origin(
    proposals: dict[ItemId, stored_state.StoredProposal], item_id: ItemId
) -> query_models.ProposalOrigin | None:
    proposal = proposals.get(item_id)
    if proposal is None:
        return None
    disposition = proposal.disposition
    return query_models.ProposalOrigin(
        str(proposal.source_task_id),
        proposal.trigger,
        proposal.relation.kind,
        str(proposal.relation.item) if proposal.relation.item is not None else None,
        proposal.why_it_matters,
        None if disposition is None else disposition.kind,
        disposition.reason
        if isinstance(
            disposition,
            released_v6_compatibility.HistoricalReturnedProposalDisposition | work_models.RejectedProposalDisposition,
        )
        else None,
    )


def _next_unstarted(items: tuple[query_models.OverviewItem, ...]) -> query_models.NextUnstarted | None:
    live_ids = frozenset(item.item_id for item in items)
    for item in items:
        if item.attempt_id is None and item.state not in {work_models.WorkState.ACTIVE, work_models.WorkState.REVIEW}:
            return query_models.NextUnstarted(
                item.item_id, tuple(dependency for dependency in item.depends_on if dependency in live_ids)
            )
    return None


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
    current_replacements: dict[ItemId, stored_state.StoredPlannedReplacement] = {}
    for relation in state.replacements.planned_replacements:
        current = current_replacements.get(relation.affected_item_id)
        if current is None or current.relation_revision < relation.relation_revision:
            current_replacements[relation.affected_item_id] = relation
    dispositions = {
        (value.affected_item_id, value.relation_revision): value for value in state.replacements.dispositions
    }

    items = tuple(
        query_models.OverviewItem(
            str(item.item_id),
            definitions[item.item_id].title,
            definitions[item.item_id].effect,
            definitions[item.item_id].unlock,
            live_state,
            item.queue_position,
            not any(link.dependency_id in live_ids for link in dependency_links[item.item_id]),
            item.timing.value if item.timing is not None else None,
            tuple(str(link.dependency_id) for link in dependency_links[item.item_id]),
            tuple(
                _dependency_reason(proposals, prerequisite_proposals, item.item_id, link.dependency_id)
                for link in dependency_links[item.item_id]
            ),
            _proposal_origin(proposals, item.item_id),
            str(attempts[item.item_id]) if item.item_id in attempts else None,
            item.next_action,
            item.source,
            item.notes,
            None
            if (relation := current_replacements.get(item.item_id)) is None
            or relation.status != work_models.PlannedReplacementStatus.CURRENT
            else query_models.PlannedReplacementWarning(
                relation.relation_revision,
                str(relation.replacement_item_id),
                relation.replacement_cost,
                (relation.affected_item_id, relation.relation_revision) in dispositions,
            ),
            _project_preparation_status(preparations.get(item.item_id), now),
        )
        for item, live_state in live_items
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (item.planned_replacement is None or item.planned_replacement.temporarily_retained)
        and (item.preparation is None or item.preparation.status != authority_models.PreparationLeaseStatus.ACTIVE)
        and (
            item.state in {work_models.WorkState.READY, work_models.WorkState.DEFERRED}
            or item.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}
        )
    )
    return query_models.WorkOverview(
        "pinboard-overview/v6",
        "sqlite-v6",
        str(state.lifecycle.project.revision),
        tuple(
            str(attempt.attempt_id)
            for attempt in sorted(state.lifecycle.attempts, key=_attempt_key)
            if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
        _next_unstarted(items),
    )


def _project_overview_item(
    item: work_models.WorkItem,
    definition: work_models.WorkItemDefinition,
    live_dependencies: frozenset[ItemId],
    proposals: dict[ItemId, stored_state.StoredProposal],
    prerequisite_proposals: dict[tuple[ItemId, ItemId], stored_state.StoredProposal],
    preparation: query_models.PreparationAuthorityStatus | None,
    replacement: work_models.PlannedReplacement | None,
    replacement_disposition: work_models.ReplacementDisposition | None,
    now: datetime,
) -> query_models.OverviewItem:
    return query_models.OverviewItem(
        str(item.item),
        definition.title,
        definition.effect,
        definition.unlock,
        item.state,
        item.queue_position,
        not any(dependency in live_dependencies for dependency in item.depends_on),
        item.timing,
        tuple(str(value) for value in item.depends_on),
        tuple(_dependency_reason(proposals, prerequisite_proposals, item.item, value) for value in item.depends_on),
        _proposal_origin(proposals, item.item),
        None if item.attempt is None else str(item.attempt),
        item.next_action,
        item.source,
        item.notes,
        None
        if replacement is None or replacement.status != work_models.PlannedReplacementStatus.CURRENT
        else query_models.PlannedReplacementWarning(
            replacement.relation_revision,
            str(replacement.replacement_item),
            replacement.replacement_cost,
            replacement_disposition is not None,
        ),
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
            definitions[item.item],
            live_ids,
            proposals,
            prerequisite_proposals,
            preparations.get(item.item),
            snapshot.current_replacement(item.item),
            None
            if (replacement := snapshot.current_replacement(item.item)) is None
            else snapshot.replacement_disposition(item.item, replacement.relation_revision),
            now,
        )
        for item in sorted(snapshot.items, key=_decision_item_key)
    )
    immediate = tuple(
        item.item_id
        for item in items
        if item.eligible
        and (item.planned_replacement is None or item.planned_replacement.temporarily_retained)
        and (item.preparation is None or item.preparation.status != authority_models.PreparationLeaseStatus.ACTIVE)
        and item.state
        in {
            work_models.WorkState.READY,
            work_models.WorkState.DEFERRED,
            work_models.WorkState.PAUSED,
            work_models.WorkState.BLOCKED,
        }
    )
    return query_models.WorkOverview(
        "pinboard-overview/v6",
        "sqlite-v6",
        snapshot.revision,
        tuple(
            str(attempt.attempt) for attempt in snapshot.attempts if attempt.state == work_models.AttemptState.ACTIVE
        ),
        items,
        immediate,
        _next_unstarted(items),
    )


def project_item_overview(facts: query_models.ItemOverviewFacts, now: datetime) -> query_models.OverviewItem:
    """Project one live item from its exact view relationships."""

    item = facts.item
    proposals, prerequisite_proposals = _proposal_maps(facts.proposals)
    live_dependencies = frozenset(dependency_id for dependency_id, is_live in facts.dependency_liveness if is_live)
    return _project_overview_item(
        item,
        facts.definition.definition,
        live_dependencies,
        proposals,
        prerequisite_proposals,
        facts.preparation,
        facts.replacement,
        facts.replacement_disposition,
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
    attempt = None if not facts.attempts else facts.attempts[0]
    attempt_state = None if attempt is None else attempt.state
    allowed_attempt_states = stored_state.allowed_current_attempt_states(item.state)
    if attempt_state not in allowed_attempt_states:
        expected = " or ".join("none" if value is None else value.value for value in allowed_attempt_states)
        observed = "none" if attempt_state is None else attempt_state.value
        return DecisionFailure(
            DecisionFailureCode.ITEM_STATUS_INCONSISTENT,
            f"Item '{item_id}' state '{item.state.value}' conflicts with current attempt state '{observed}'.",
            FailureDetails(
                observed=(
                    FailureFact("item_id", str(item.item_id)),
                    FailureFact("item_state", item.state.value),
                    FailureFact("item_timing", None if item.timing is None else item.timing.value),
                    FailureFact("item_outcome_evidence", item.outcome_evidence),
                    FailureFact("item_next_action", item.next_action),
                    FailureFact("item_source", item.source),
                    FailureFact("item_notes", item.notes),
                    FailureFact("item_queue_position", item.queue_position),
                    FailureFact("attempt_id", None if attempt is None else str(attempt.attempt_id)),
                    FailureFact("attempt_state", observed),
                    FailureFact("attempt_candidate_revision", None if attempt is None else attempt.candidate_revision),
                ),
                mismatches=(FailureMismatch("current_attempt_state", expected, observed),),
                retry=RetryDisposition.DO_NOT_RETRY,
                effect=EffectDisposition.UNCHANGED,
                changed_surfaces=(),
                alternatives=(),
            ),
        )
    attempts = tuple(
        query_models.ItemStatusAttempt(str(attempt.attempt_id), attempt.state, attempt.candidate_revision)
        for attempt in facts.attempts
    )
    return query_models.ItemStatus(
        "pinboard-item-status/v1",
        "sqlite-v6",
        str(facts.project_revision),
        str(item.item_id),
        facts.definition_title,
        stored_state.StoredWorkItemState.READY if item.state == stored_state.StoredWorkItemState.INTAKE else item.state,
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
        "pinboard-work-item-definition/v2",
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
        definition.checkout_policy,
        tuple(
            query_models.WorkObligationView(
                obligation.obligation_id,
                obligation.statement,
                obligation.deferral_policy,
            )
            for obligation in definition.obligations
        ),
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
        "sqlite-v6",
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
        "sqlite-v6",
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


def present_parallel_preview(preview: query_models.ParallelPreview) -> query_models.ParallelPreviewView:
    """Preserve the neutral v1 launchable/excluded grouping for sibling transports."""
    launchable: list[query_models.ParallelItemView] = []
    excluded: list[query_models.ParallelItemView] = []
    for item in preview.items:
        match item:
            case query_models.LaunchableParallelItem():
                launchable.append(
                    query_models.ParallelItemView(
                        item.item_id, item.label, item.state.value, item.attempt_id, "launchable", ()
                    )
                )
            case query_models.ExcludedParallelItem(reasons=reasons):
                excluded.append(
                    query_models.ParallelItemView(
                        item.item_id, item.label, item.state.value, item.attempt_id, "excluded", reasons
                    )
                )
            case _ as unreachable:
                assert_never(unreachable)
    match preview.selection:
        case query_models.ParallelSelection.SELECTED:
            selection = "selected"
        case query_models.ParallelSelection.ALL_SAFE:
            selection = "all-safe"
        case _ as unreachable:
            assert_never(unreachable)
    return query_models.ParallelPreviewView(
        "pinboard-parallel-preview/v1", preview.revision, selection, preview.safe, tuple(launchable), tuple(excluded)
    )
