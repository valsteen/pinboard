"""Project read-only application views from exact capabilities or stored snapshots.

Callers own SQLite access and time sampling. Exact status use cases request only
their operation facts; remaining projections select from an already-loaded complete
snapshot. These functions never read files, mutate state, or present output.
"""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import assert_never

import msgspec

from pinboard.application import (
    checkpoint_packages,
    ports,
    query_models,
    released_v6_compatibility,
    stored_state,
    work_brief_models,
)
from pinboard.domain import authority_models, decision_models, history, work_models
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
from pinboard.domain.identifiers import AttemptId, CandidateId, HistoryId, TaskId, WorkItemId
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


def decode_recorded_pause_reason(
    attempt_id: AttemptId,
    attempt_state: work_models.AttemptState,
    history_id: HistoryId,
    committed_at: datetime,
    action_kind: decision_models.ActionKind | released_v6_compatibility.HistoricalActionKind,
    outcome_schema: str,
    outcome_payload: bytes,
) -> query_models.RecordedPauseReason:
    """Return the human pause reason carried by a paused attempt's latest receipt.

    The caller supplies only the receipt whose project revision is the attempt's
    subject revision. A pause keeps its reason as receipt evidence and a paused
    rebind carries it forward; checkpoint acceptance and every other receipt
    present no pause reason. A pause or rebind receipt whose outcome does not
    decode is returned as damaged for the consuming read to name.
    """

    if attempt_state != work_models.AttemptState.PAUSED:
        return None
    match action_kind:
        case decision_models.ActionKind.PAUSE | decision_models.ActionKind.REBIND_ATTEMPT as pause_action:
            if outcome_schema != "transition-receipt/v1":
                defect = f"The pause outcome uses {outcome_schema!r} instead of transition-receipt/v1."
            else:
                try:
                    return (
                        msgspec.json.Decoder(history.TransitionReceiptOutcome, strict=True)
                        .decode(outcome_payload)
                        .evidence
                    )
                except msgspec.DecodeError as error:
                    defect = f"The pause outcome does not decode as transition-receipt/v1: {error}"
            return query_models.DamagedTransitionReceipt(attempt_id, history_id, committed_at, pause_action, defect)
        case _:
            return None


def damaged_receipt_message(damaged: query_models.DamagedTransitionReceipt) -> str:
    return (
        f"Transition receipt {damaged.history_id} ({damaged.action_kind.value}, committed "
        f"{damaged.committed_at.isoformat()}) for attempt '{damaged.attempt_id}' is damaged: {damaged.defect}"
    )


def damaged_receipt_diagnosis(
    action_kind: query_models.DamagedReceiptActionKind,
) -> query_models.DamagedReceiptDiagnosis:
    """Name the read that can diagnose a damaged consumed receipt without repairing it.

    Validation derives every item view, which decodes a paused attempt's pause or
    rebind receipt, so it names that damage. Item-view derivation does not decode
    a review-verdict receipt, so validation does not name its damage and the read
    reports the receipt and defect for the human to diagnose.
    """

    match action_kind:
        case decision_models.ActionKind.PAUSE | decision_models.ActionKind.REBIND_ATTEMPT:
            return query_models.DamagedReceiptDiagnosis.VALIDATION
        case (
            decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACCEPT_CHECKPOINT
        ):
            return query_models.DamagedReceiptDiagnosis.HUMAN
        case _ as unreachable:
            assert_never(unreachable)


def damaged_receipt_recovery(damaged: query_models.DamagedTransitionReceipt) -> str:
    match damaged_receipt_diagnosis(damaged.action_kind):
        case query_models.DamagedReceiptDiagnosis.VALIDATION:
            return (
                f"Report damaged transition receipt {damaged.history_id} to the human and diagnose it with "
                "'pinboard validate --json'; Pinboard does not repair receipts, so do not edit the ledger or retry "
                "this read."
            )
        case query_models.DamagedReceiptDiagnosis.HUMAN:
            return (
                f"Report to the human that transition receipt {damaged.history_id} "
                f"({damaged.action_kind.value}, committed {damaged.committed_at.isoformat()}) for attempt "
                f"'{damaged.attempt_id}' is damaged: {damaged.defect.rstrip('.')}. Pinboard does not repair receipts; do not "
                "retry this read or edit the ledger."
            )
        case _ as unreachable:
            assert_never(unreachable)


def damaged_receipt_validation_recovery(damaged: query_models.DamagedTransitionReceipt) -> str:
    return (
        f"Report damaged transition receipt {damaged.history_id} to the human for diagnosis. "
        "Pinboard does not repair receipts; do not edit the ledger or rerun validation."
    )


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
        context.work_item_id,
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
                context.work_item_id,
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
            item = context.work_item
            attempt_record = work_models.AttemptRecord(
                context.attempt_id,
                context.work_item_id,
                context.state,
                context.accepted_scope_revision,
                context.accepted_scope_digest,
                None if context.candidate_revision is None else CandidateId(context.candidate_revision),
                context.brief_artifact_ref_id,
                pause_reason=context.pause_reason,
            )
            groups = project_attempt_action_groups(
                work_models.ProjectAttemptActionContext(
                    item.work_item_id,
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
                context.work_item_id,
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
                    return query_models.PausedAttemptContinuation(*continuation_arguments, context.pause_reason)
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
) -> DecisionResult[
    query_models.ActionContinuation
    | query_models.RepositoryDispositionContinuation
    | query_models.CommitThenReinspectContinuation
]:
    if candidate_lineage == query_models.CandidateLineage.COMMIT_CURRENT:
        return query_models.RepositoryDispositionContinuation(reconciliation.target_revision, reconciliation.relation)
    if candidate_lineage == query_models.CandidateLineage.WORKING_TREE_CURRENT:
        return query_models.CommitThenReinspectContinuation(reconciliation.target_revision, reconciliation.relation)
    for action in actions:
        if isinstance(action, decision_models.ReturnForCorrectionAction):
            condition = (
                "The protected candidate no longer matches the checkout. Apply return-for-correction to the same "
                "attempt with this lineage mismatch as the reason, preserve its history_id, obtain correction-source "
                "review, then dispatch correction work that submits a current clean commit candidate; no user input "
                "is required."
            )
            return query_models.ActionContinuation(decision_models.action_id(action), action.kind, condition)
    return DecisionFailure(
        DecisionFailureCode.ACTION_NOT_AVAILABLE,
        "Candidate lineage correction is not currently available.",
        None,
    )


def select_resumed_review_operation(  # noqa: C901, PLR0912 - distinct reviewed-candidate relations remain explicit
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
    match relation:
        case query_models.IntegrationRelation.CANDIDATE_INTEGRATED:
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
        case _ as unreachable:
            assert_never(unreachable)


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
            return query_models.ReconcileRepositoryContinuation(
                context.candidate_revision,
                "The candidate is favorably reviewed and not yet reconciled with the repository. Observe the "
                "integration target, the candidate's relation to it, the repository phase, and the runtime effects "
                "that phase needs; then inspect again with those reconciliation facts to select disposition, "
                "refresh, correction, cleanup, or completion.",
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
    if context.work_item.live_dependencies:
        return query_models.DependencyContinuation(tuple(str(value) for value in context.work_item.live_dependencies))
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
    return value.queue_position if value.queue_position is not None else 0, str(value.work_item_id)


def _select_live_items(
    state: stored_state.StoredWorkState,
) -> tuple[tuple[stored_state.StoredWorkItem, work_models.WorkState], ...]:
    return tuple(
        (item, live_state)
        for item in sorted(state.lifecycle.work_items, key=_item_key)
        if (live_state := stored_state.live_work_state(item.state)) is not None
    )


def _parallel_item_key(value: query_models.ParallelItem) -> str:
    return value.item_id


def _project_preparation_status(
    retained: tuple[stored_state.StoredPreparationLease, stored_state.PreparationLeaseGeneration] | None,
    now: datetime,
) -> query_models.PreparationStatusView | None:
    if retained is None:
        return None
    lease, anchor = retained
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
    Mapping[WorkItemId, stored_state.StoredProposal],
    Mapping[tuple[WorkItemId, WorkItemId], stored_state.StoredProposal],
]:
    by_item = {WorkItemId(proposal.proposal_id): proposal for proposal in proposals}
    prerequisites = {
        (proposal.relation.work_item_id, WorkItemId(proposal.proposal_id)): proposal
        for proposal in proposals
        if isinstance(proposal.relation, work_models.PrerequisiteProposalRelation)
    }
    return MappingProxyType(by_item), MappingProxyType(prerequisites)


def _dependency_reason(
    proposals: Mapping[WorkItemId, stored_state.StoredProposal],
    prerequisite_proposals: Mapping[tuple[WorkItemId, WorkItemId], stored_state.StoredProposal],
    work_item_id: WorkItemId,
    dependency_id: WorkItemId,
) -> query_models.DependencyReason:
    proposal = proposals.get(work_item_id)
    if (
        proposal is not None
        and isinstance(proposal.relation, work_models.FollowUpProposalRelation)
        and proposal.relation.work_item_id == dependency_id
    ):
        reason = f"Follow-up to {dependency_id}: {proposal.why_it_matters}"
    else:
        prerequisite = prerequisite_proposals.get((work_item_id, dependency_id))
        reason = (
            f"Inferred prerequisite {dependency_id}: {prerequisite.why_it_matters}"
            if prerequisite is not None
            else "Recorded dependency."
        )
    return query_models.DependencyReason(str(dependency_id), reason)


def _proposal_origin(
    proposals: Mapping[WorkItemId, stored_state.StoredProposal], work_item_id: WorkItemId
) -> query_models.ProposalOrigin | None:
    proposal = proposals.get(work_item_id)
    if proposal is None:
        return None
    disposition = proposal.disposition
    return query_models.ProposalOrigin(
        str(proposal.source_task_id),
        proposal.trigger,
        proposal.relation.kind,
        str(proposal.relation.work_item_id) if proposal.relation.work_item_id is not None else None,
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
        match item.state:
            case work_models.WorkState.READY | work_models.WorkState.BLOCKED | work_models.WorkState.DEFERRED:
                if item.attempt_id is None:
                    return query_models.NextUnstarted(
                        item.item_id, tuple(dependency for dependency in item.depends_on if dependency in live_ids)
                    )
            case work_models.WorkState.ACTIVE | work_models.WorkState.PAUSED | work_models.WorkState.REVIEW:
                pass
            case _ as unreachable:
                assert_never(unreachable)
    return None


def present_overview(
    revision: str,
    active_attempts: tuple[str, ...],
    items: tuple[query_models.OverviewItem, ...],
    board: query_models.BoardPages,
) -> query_models.WorkOverview:
    """Assemble the overview from live items in saved order and the selected work root's board pages."""

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
        "pinboard-overview/v7",
        "sqlite-v7",
        revision,
        active_attempts,
        items,
        immediate,
        _next_unstarted(items),
        board,
    )


def project_portfolio(state: stored_state.StoredWorkState, now: datetime) -> tuple[query_models.OverviewItem, ...]:
    """Project every live item in saved order from complete state, as generated views render it."""

    definitions = {value.item_id: value.definition for value in state.lifecycle.definition_revisions}
    attempts = {
        attempt.item_id: attempt.attempt_id
        for attempt in state.lifecycle.attempts
        if attempt.state != work_models.AttemptState.DONE
    }
    dependency_groups: dict[WorkItemId, list[stored_state.ItemDependency]] = {
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
        lease.item_id: (lease, preparation_anchors[(lease.item_id, lease.generation)])
        for lease in state.authority.preparation_leases
    }
    live_items = _select_live_items(state)
    live_ids = frozenset(item.item_id for item, _live_state in live_items)
    current_replacements: dict[WorkItemId, stored_state.StoredPlannedReplacement] = {}
    for relation in state.replacements.planned_replacements:
        current = current_replacements.get(relation.affected_item_id)
        if current is None or current.relation_revision < relation.relation_revision:
            current_replacements[relation.affected_item_id] = relation
    dispositions = {
        (value.affected_item_id, value.relation_revision): value for value in state.replacements.dispositions
    }

    return tuple(
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


def _project_overview_item(
    item: work_models.WorkItem,
    definition: work_models.WorkItemDefinition,
    live_dependencies: frozenset[WorkItemId],
    proposals: Mapping[WorkItemId, stored_state.StoredProposal],
    prerequisite_proposals: Mapping[tuple[WorkItemId, WorkItemId], stored_state.StoredProposal],
    preparation: query_models.PreparationAuthorityStatus | None,
    replacement: work_models.PlannedReplacement | None,
    replacement_disposition: work_models.ReplacementDisposition | None,
    now: datetime,
) -> query_models.OverviewItem:
    return query_models.OverviewItem(
        str(item.work_item_id),
        definition.title,
        definition.effect,
        definition.unlock,
        item.state,
        item.queue_position,
        not any(dependency in live_dependencies for dependency in item.depends_on),
        item.timing,
        tuple(str(value) for value in item.depends_on),
        tuple(
            _dependency_reason(proposals, prerequisite_proposals, item.work_item_id, value) for value in item.depends_on
        ),
        _proposal_origin(proposals, item.work_item_id),
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


def _project_current_items(
    facts: query_models.ProjectOverviewFacts, now: datetime
) -> tuple[query_models.OverviewItem, ...]:
    snapshot = facts.snapshot
    definitions = {value.work_item_id: value.definition for value in snapshot.definitions}
    live_ids = frozenset(item.work_item_id for item in snapshot.items)
    proposals, prerequisite_proposals = _proposal_maps(facts.proposals)
    preparations = {value.work_item_id: value for value in facts.preparations}

    return tuple(
        _project_overview_item(
            item,
            definitions[item.work_item_id],
            live_ids,
            proposals,
            prerequisite_proposals,
            preparations.get(item.work_item_id),
            snapshot.current_replacement(item.work_item_id),
            None
            if (replacement := snapshot.current_replacement(item.work_item_id)) is None
            else snapshot.replacement_disposition(item.work_item_id, replacement.relation_revision),
            now,
        )
        for item in sorted(snapshot.items, key=_decision_item_key)
    )


def project_current_overview(
    facts: query_models.ProjectOverviewFacts, now: datetime, board: query_models.BoardPages
) -> query_models.WorkOverview:
    """Project overview output from current facts that exclude retained history."""

    return present_overview(
        facts.snapshot.revision,
        tuple(
            str(attempt.attempt)
            for attempt in facts.snapshot.attempts
            if attempt.state == work_models.AttemptState.ACTIVE
        ),
        _project_current_items(facts, now),
        board,
    )


def project_live_portfolio(
    facts: query_models.LivePortfolioFacts, now: datetime
) -> tuple[query_models.OverviewItem, ...]:
    """Project live items for generated presentations, which never render preparation authority."""

    return _project_current_items(query_models.ProjectOverviewFacts(facts.snapshot, facts.proposals, ()), now)


def project_item_overview(facts: query_models.ItemOverviewFacts, now: datetime) -> query_models.OverviewItem:
    """Project one live item from its exact view relationships."""

    item = facts.work_item
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


def _stored_receipt(
    attempt_id: AttemptId,
    receipt: query_models.ConsumedTransitionReceipt,
    action_kind: query_models.DamagedReceiptActionKind,
) -> stored_state.StoredTransitionReceipt | query_models.DamagedTransitionReceipt:
    for column, text in (("input_json", receipt.input_json), ("outcome_json", receipt.outcome_json)):
        try:
            msgspec.json.decode(text.encode("utf-8"), type=msgspec.Raw)
        except msgspec.DecodeError as error:
            return _damaged(attempt_id, receipt, action_kind, f"Column {column!r} is not valid JSON: {error}")
    return stored_state.StoredTransitionReceipt(
        receipt.history_id,
        receipt.project_revision,
        receipt.action_id,
        receipt.action_kind,
        receipt.subject_id,
        receipt.artifact_ref_id,
        receipt.authorization,
        receipt.actor_task_id,
        receipt.actor_host_id,
        receipt.input_schema,
        work_models.CanonicalJson(receipt.input_json.encode("utf-8")),
        receipt.outcome_schema,
        work_models.CanonicalJson(receipt.outcome_json.encode("utf-8")),
        receipt.committed_at,
    )


def _damaged(
    attempt_id: AttemptId,
    receipt: query_models.ConsumedTransitionReceipt,
    action_kind: query_models.DamagedReceiptActionKind,
    defect: str,
) -> query_models.DamagedTransitionReceipt:
    return query_models.DamagedTransitionReceipt(
        attempt_id, receipt.history_id, receipt.committed_at, action_kind, defect
    )


def _historical_receipt_outcome(
    receipt: query_models.ConsumedTransitionReceipt,
) -> history.TransitionReceiptOutcome | None:
    """Read a retained historical receipt's outcome when it decodes; its absence is never damage."""

    if receipt.outcome_schema != "transition-receipt/v1":
        return None
    try:
        return msgspec.json.decode(
            receipt.outcome_json.encode("utf-8"), type=history.TransitionReceiptOutcome, strict=True
        )
    except msgspec.DecodeError:
        return None


def _returned_verdict(
    attempt_id: AttemptId, event: query_models.ReviewEventFacts
) -> query_models.ReturnedForCorrectionVerdict | query_models.DamagedTransitionReceipt:
    receipt = event.receipt
    returned = decision_models.ActionKind.RETURN_FOR_CORRECTION
    match receipt.input_schema:
        case "return-for-correction/v1":
            stored = _stored_receipt(attempt_id, receipt, returned)
            if isinstance(stored, query_models.DamagedTransitionReceipt):
                return stored
            outcome = checkpoint_packages.decode_correction_outcome(stored, str(attempt_id))
            if isinstance(outcome, DecisionFailure):
                return _damaged(attempt_id, receipt, returned, outcome.message)
            reason = outcome.evidence
        case "decision/v1":
            historical = _historical_receipt_outcome(receipt)
            reason = None if historical is None else historical.evidence
        case _:
            return _damaged(attempt_id, receipt, returned, f"Unsupported return input schema {receipt.input_schema!r}.")
    return query_models.ReturnedForCorrectionVerdict(int(receipt.history_id), reason, event.rebound_since)


def _continued_verdict(
    attempt_id: AttemptId, receipt: query_models.ConsumedTransitionReceipt
) -> query_models.AcceptedAndContinuedVerdict | query_models.DamagedTransitionReceipt:
    continued = decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
    if receipt.outcome_schema != "transition-receipt/v1":
        return _damaged(attempt_id, receipt, continued, f"Unsupported outcome schema {receipt.outcome_schema!r}.")
    try:
        outcome = msgspec.json.decode(
            receipt.outcome_json.encode("utf-8"), type=history.TransitionReceiptOutcome, strict=True
        )
    except msgspec.DecodeError as error:
        return _damaged(
            attempt_id, receipt, continued, f"The outcome does not decode as transition-receipt/v1: {error}"
        )
    if outcome.outcome != continued.value:
        return _damaged(
            attempt_id, receipt, continued, "The outcome does not record review acceptance and continuation."
        )
    return query_models.AcceptedAndContinuedVerdict(outcome.evidence)


def _checkpoint_verdict(
    attempt_id: AttemptId, receipt: query_models.ConsumedTransitionReceipt
) -> query_models.CheckpointAcceptedVerdict | query_models.DamagedTransitionReceipt:
    accepted = decision_models.ActionKind.ACCEPT_CHECKPOINT
    match receipt.outcome_schema:
        case "checkpoint-acceptance/v2":
            try:
                outcome = msgspec.json.decode(
                    receipt.outcome_json.encode("utf-8"), type=history.CheckpointAcceptanceOutcome, strict=True
                )
            except msgspec.DecodeError as error:
                return _damaged(
                    attempt_id, receipt, accepted, f"The outcome does not decode as checkpoint-acceptance/v2: {error}"
                )
            return query_models.CheckpointAcceptedVerdict(outcome.checkpoint)
        case "transition-receipt/v1":
            historical = _historical_receipt_outcome(receipt)
            return query_models.CheckpointAcceptedVerdict(None if historical is None else historical.checkpoint)
        case _:
            return _damaged(attempt_id, receipt, accepted, f"Unsupported outcome schema {receipt.outcome_schema!r}.")


def _review_verdict(
    attempt: query_models.ItemStatusAttemptFacts,
    reviews: ports.ReadyCandidateReviewReader,
) -> query_models.ReviewVerdict | query_models.DamagedTransitionReceipt:
    """Derive the current attempt's verdict from its latest review-relevant receipt."""

    event = attempt.review_event
    if event is None:
        return query_models.NoReviewVerdict()
    match event.action_kind:
        case decision_models.ActionKind.SUBMIT_REVIEW:
            if attempt.state != work_models.AttemptState.REVIEW:
                return query_models.NoReviewVerdict()
            ready = reviews.read_ready_candidate_review(attempt.attempt_id)
            if ready is None:
                return query_models.NoReviewVerdict()
            reference = ready.reference
            return query_models.ReadyReviewVerdict(
                ready.candidate_revision,
                query_models.CandidateReviewArtifact(
                    int(reference.artifact_ref_id),
                    reference.selector,
                    reference.content_sha256,
                    reference.size_bytes,
                    reference.accepted_revision,
                ),
            )
        case decision_models.ActionKind.RETURN_FOR_CORRECTION:
            return _returned_verdict(attempt.attempt_id, event)
        case decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE:
            return _continued_verdict(attempt.attempt_id, event.receipt)
        case decision_models.ActionKind.ACCEPT_CHECKPOINT:
            return _checkpoint_verdict(attempt.attempt_id, event.receipt)
        case _ as unreachable:
            assert_never(unreachable)


def _closure_action(
    kind: decision_models.ActionKind | released_v6_compatibility.HistoricalActionKind,
) -> query_models.ItemClosureAction | None:
    match kind:
        case decision_models.ActionKind.COMPLETE:
            return query_models.ItemClosureAction.COMPLETE
        case decision_models.ActionKind.CLOSE:
            return query_models.ItemClosureAction.CLOSE
        case decision_models.ActionKind.MERGE_PROPOSAL:
            return query_models.ItemClosureAction.MERGE_PROPOSAL
        case decision_models.ActionKind.CLOSE_PR_REVIEW:
            return query_models.ItemClosureAction.CLOSE_PR_REVIEW
        case (
            decision_models.ActionKind.START_PR_REVIEW
            | decision_models.ActionKind.REVIEW_PR_BRIEF
            | decision_models.ActionKind.OBSERVE_PR_HEAD
            | decision_models.ActionKind.RECORD_PR_ROUND
            | decision_models.ActionKind.ACCEPT_CHECKPOINT
            | decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
            | decision_models.ActionKind.ACTIVATE
            | decision_models.ActionKind.BLOCK
            | decision_models.ActionKind.BLOCK_ITEM
            | decision_models.ActionKind.CONTINUE
            | decision_models.ActionKind.DEFER
            | decision_models.ActionKind.DISPATCH
            | decision_models.ActionKind.INSPECT
            | decision_models.ActionKind.PAUSE
            | decision_models.ActionKind.REJECT_PROPOSAL
            | decision_models.ActionKind.REOPEN
            | decision_models.ActionKind.RECORD_REPLACEMENT
            | decision_models.ActionKind.REBIND_ATTEMPT
            | decision_models.ActionKind.REPORT_BLOCKER
            | decision_models.ActionKind.RESUME
            | decision_models.ActionKind.RETURN_FOR_CORRECTION
            | decision_models.ActionKind.RETAIN_TEMPORARILY
            | decision_models.ActionKind.REVISE_ITEM
            | decision_models.ActionKind.SUBMIT_REVIEW
            | released_v6_compatibility.HistoricalActionKind.ACCEPT_PROPOSAL
            | released_v6_compatibility.HistoricalActionKind.MARK_READY
            | released_v6_compatibility.HistoricalActionKind.RETURN_PROPOSAL
        ):
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _project_closure(facts: query_models.ItemClosureFacts | None) -> query_models.ItemClosure | None:
    """Project closure from receipt row columns only; a non-terminal receipt yields no closure."""

    if facts is None or (action := _closure_action(facts.action_kind)) is None:
        return None
    closing = facts.closing_attempt
    return query_models.ItemClosure(
        action,
        facts.committed_at.isoformat(),
        None
        if closing is None
        else query_models.ClosingAttempt(str(closing.attempt_id), closing.branch, closing.candidate_revision),
    )


def presented_item_state(value: stored_state.StoredWorkItemState) -> stored_state.StoredWorkItemState:
    """Present released v6 intake state as the current ready state."""

    return stored_state.StoredWorkItemState.READY if value == stored_state.StoredWorkItemState.INTAKE else value


def project_item_status(
    reader: ports.ItemStatusReader,
    reviews: ports.ReadyCandidateReviewReader,
    work_item_id: WorkItemId,
    now: datetime,
    board: query_models.BoardPages,
) -> DecisionResult[query_models.ItemStatus] | query_models.DamagedTransitionReceipt:
    facts = reader.read_item_status(work_item_id)
    if facts is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{work_item_id}' was not found.", None)
    item = facts.work_item
    if facts.definition_title is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID, f"Item '{work_item_id}' has no definition.", None
        )
    attempt = None if not facts.attempts else facts.attempts[0]
    attempt_state = None if attempt is None else attempt.state
    allowed_attempt_states = stored_state.allowed_current_attempt_states(item.state)
    if attempt_state not in allowed_attempt_states:
        expected = " or ".join("none" if value is None else value.value for value in allowed_attempt_states)
        observed = "none" if attempt_state is None else attempt_state.value
        return DecisionFailure(
            DecisionFailureCode.ITEM_STATUS_INCONSISTENT,
            f"Item '{work_item_id}' state '{item.state.value}' conflicts with current attempt state '{observed}'.",
            FailureDetails(
                observed=(
                    FailureFact("item_id", str(item.work_item_id)),
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
    attempts: list[query_models.ItemStatusAttempt] = []
    verdict: query_models.ReviewVerdict = query_models.NoReviewVerdict()
    for selected in facts.attempts:
        if isinstance(selected.pause_reason, query_models.DamagedTransitionReceipt):
            return selected.pause_reason
        attempts.append(
            query_models.ItemStatusAttempt(
                str(selected.attempt_id),
                selected.state,
                selected.branch,
                selected.candidate_revision,
                selected.pause_reason,
            )
        )
        selected_verdict = _review_verdict(selected, reviews)
        if isinstance(selected_verdict, query_models.DamagedTransitionReceipt):
            return selected_verdict
        verdict = selected_verdict
    return query_models.ItemStatus(
        "pinboard-item-status/v3",
        "sqlite-v7",
        str(facts.project_revision),
        str(item.work_item_id),
        facts.definition_title,
        presented_item_state(item.state),
        item.timing,
        item.outcome_evidence,
        item.source,
        item.queue_position,
        query_models.IntakeContext("original-context", item.next_action, item.notes),
        tuple(attempts),
        verdict,
        _project_closure(facts.closure),
        _project_selected_preparation_status(facts.preparation, now),
        board,
    )


def project_branch_owners(reader: ports.BranchOwnerReader, branch: str) -> query_models.BranchOwners | None:
    """Return every retained item and attempt that recorded this exact branch, or None when none did."""

    facts = reader.read_branch_owners(branch)
    if not facts.owners:
        return None
    return query_models.BranchOwners(
        "pinboard-branch-owners/v1",
        "sqlite-v7",
        str(facts.project_revision),
        branch,
        tuple(
            query_models.BranchOwner(
                str(owner.work_item_id), owner.item_state, str(owner.attempt_id), owner.attempt_state
            )
            for owner in facts.owners
        ),
    )


def _checkpoint_selection(
    work_item_id: WorkItemId,
    state: stored_state.StoredWorkItemState,
    attempt_id: AttemptId,
    checkpoint: query_models.IntegrationCheckpointFacts,
) -> (
    query_models.CheckpointSelection
    | query_models.IntegrationCandidateUnavailable
    | query_models.DamagedTransitionReceipt
):
    accepted = decision_models.ActionKind.ACCEPT_CHECKPOINT
    stored = _stored_receipt(attempt_id, checkpoint.receipt, accepted)
    if isinstance(stored, query_models.DamagedTransitionReceipt):
        return stored
    try:
        outcome = msgspec.json.decode(
            bytes(stored.outcome_payload), type=history.CheckpointAcceptanceOutcome, strict=True
        )
    except msgspec.DecodeError as error:
        return _damaged(
            attempt_id,
            checkpoint.receipt,
            accepted,
            f"The outcome does not decode as checkpoint-acceptance/v2: {error}",
        )
    if outcome.outcome != accepted.value:
        return _damaged(attempt_id, checkpoint.receipt, accepted, "The outcome does not record checkpoint acceptance.")
    if checkpoint.package_reference is None:
        return query_models.IntegrationCandidateUnavailable(
            work_item_id,
            state,
            attempt_id,
            query_models.IntegrationUnavailableReason.CHECKPOINT_WITHOUT_CANDIDATE_SNAPSHOT,
        )
    return query_models.CheckpointSelection(
        attempt_id, work_item_id, outcome.checkpoint, outcome.candidate, stored, checkpoint.package_reference
    )


def select_integration_source(
    facts: query_models.IntegrationFacts,
) -> (
    query_models.IntegrationSourceSelection
    | query_models.IntegrationCandidateUnavailable
    | query_models.DamagedTransitionReceipt
):
    """Select the item's reviewed candidate whose accepted diff an integration check compares.

    A live attempt's protected candidate wins; otherwise that attempt's latest checkpoint
    acceptance applies. A done item uses its completion's closing candidate. Accepted-and-
    continued candidates and earlier checkpoints are never selected.
    """

    state = presented_item_state(facts.state)
    unavailable = query_models.IntegrationUnavailableReason
    if stored_state.live_work_state(facts.state) is None:
        closing = facts.closing_attempt
        if facts.closure_action is None:
            return query_models.IntegrationCandidateUnavailable(
                facts.work_item_id, state, None, unavailable.CLOSURE_UNKNOWN
            )
        if facts.closure_action != decision_models.ActionKind.COMPLETE:
            return query_models.IntegrationCandidateUnavailable(
                facts.work_item_id, state, None, unavailable.CLOSED_WITHOUT_COMPLETION
            )
        if closing is None:
            return query_models.IntegrationCandidateUnavailable(
                facts.work_item_id, state, None, unavailable.NO_REVIEWED_CANDIDATE
            )
        if closing.candidate_snapshot is None:
            return query_models.IntegrationCandidateUnavailable(
                facts.work_item_id,
                state,
                closing.attempt_id,
                unavailable.NO_REVIEWED_CANDIDATE
                if closing.candidate_revision is None
                else unavailable.PRE_SNAPSHOT_CANDIDATE,
            )
        return query_models.CompletionSelection(closing.candidate_snapshot)
    attempt = facts.current_attempt
    if attempt is None:
        return query_models.IntegrationCandidateUnavailable(
            facts.work_item_id, state, None, unavailable.NO_REVIEWED_CANDIDATE
        )
    if attempt.candidate_revision is not None:
        if attempt.candidate_snapshot is None:
            return query_models.IntegrationCandidateUnavailable(
                facts.work_item_id, state, attempt.attempt_id, unavailable.PRE_SNAPSHOT_CANDIDATE
            )
        return query_models.ProtectedReviewSelection(attempt.candidate_snapshot)
    if attempt.latest_checkpoint is None:
        return query_models.IntegrationCandidateUnavailable(
            facts.work_item_id, state, attempt.attempt_id, unavailable.NO_REVIEWED_CANDIDATE
        )
    return _checkpoint_selection(facts.work_item_id, state, attempt.attempt_id, attempt.latest_checkpoint)


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
    reader: ports.ItemDefinitionReader, work_item_id: WorkItemId
) -> DecisionResult[query_models.ItemDefinition]:
    selected = reader.read_item_definition(work_item_id)
    if selected.item_subject_revision is None:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{work_item_id}' does not exist.", None)
    if selected.definition is None:
        return DecisionFailure(
            DecisionFailureCode.ITEM_DEFINITION_INVALID,
            f"Item '{work_item_id}' has no accepted definition.",
            None,
        )
    return query_models.ItemDefinition(
        "pinboard-item-definition/v1",
        "sqlite-v7",
        selected.project_revision,
        work_item_id,
        selected.item_subject_revision,
        selected.definition.revision,
        selected.definition.digest,
        _project_definition(selected.definition.definition),
    )


def select_item_definition_history(
    reader: ports.ItemDefinitionReader,
    work_item_id: WorkItemId,
    *,
    limit: int,
    before_revision: int | None,
) -> DecisionResult[query_models.ItemDefinitionHistory]:
    selected = reader.read_item_definition_history(work_item_id, limit=limit, before_revision=before_revision)
    if not selected.item_exists:
        return DecisionFailure(DecisionFailureCode.ITEM_NOT_FOUND, f"Item '{work_item_id}' does not exist.", None)
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
        "sqlite-v7",
        selected.project_revision,
        work_item_id,
        rows,
        rows[-1].revision if len(selected.revisions) > limit else None,
    )


def _classify_parallel_exclusion_reasons(
    item: query_models.ParallelPreviewItemFacts,
    operation_time: datetime,
) -> tuple[query_models.ParallelReason, ...]:
    item_id = item.work_item_id
    match item.state:
        case work_models.WorkState.READY | work_models.WorkState.ACTIVE:
            pass
        case (
            work_models.WorkState.PAUSED
            | work_models.WorkState.BLOCKED
            | work_models.WorkState.DEFERRED
            | work_models.WorkState.REVIEW
        ):
            return (
                query_models.ParallelReason(
                    query_models.ParallelReasonCode.STATE_NOT_LAUNCHABLE,
                    f"Item '{item_id}' is {item.state.value}; only ready items and unowned active attempts can launch.",
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
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
            str(item.work_item_id),
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

    definitions = {value.work_item_id: value.definition for value in snapshot.definitions}
    live_ids = frozenset(item.work_item_id for item in snapshot.items)
    attempts = {value.attempt: value for value in snapshot.attempts}
    attempt_authorities = {value.attempt: value for value in snapshot.command_attempt_authorities}
    preparations = {value.work_item_id: value for value in snapshot.command_preparation_authorities}
    items: list[query_models.ParallelPreviewItemFacts] = []
    for item in snapshot.items:
        command_preparation = preparations.get(item.work_item_id)
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
                item.work_item_id,
                definitions[item.work_item_id].title,
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
    facts = reader.read_parallel_preview(tuple(WorkItemId(item_id) for item_id in selected))
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
