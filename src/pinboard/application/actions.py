"""Discover currently legal actions from one already-loaded stored snapshot.

The caller owns SQLite access and time sampling. This module projects the
snapshot into domain decision facts and asks the domain for legal actions.
"""

from datetime import datetime
from typing import assert_never

from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, ProposalId


def action_subject_ids(
    action: decision_models.Action,
) -> tuple[tuple[ItemId, ...], tuple[AttemptId, ...], tuple[ProposalId, ...]]:
    """Return the exact persisted subject family selected by one action."""

    match action:
        case (
            decision_models.AcceptCheckpointAction(capability=capability)
            | decision_models.AcceptReviewAndContinueAction(capability=capability)
            | decision_models.BlockAttemptAction(capability=capability)
            | decision_models.CompleteAction(capability=capability)
            | decision_models.ContinueAction(capability=capability)
            | decision_models.DispatchAction(capability=capability)
            | decision_models.PauseAction(capability=capability)
            | decision_models.ReportBlockerAction(capability=capability)
            | decision_models.ReturnForCorrectionAction(capability=capability)
            | decision_models.SubmitReviewAction(capability=capability)
        ):
            return (), (capability.subject,), ()
        case (
            decision_models.ActivateAction(capability=capability)
            | decision_models.BlockItemAction(capability=capability)
            | decision_models.CloseAction(capability=capability)
            | decision_models.DeferAction(capability=capability)
            | decision_models.MarkReadyAction(capability=capability)
            | decision_models.ReopenAction(capability=capability)
            | decision_models.ResumeAction(capability=capability)
            | decision_models.ReviseItemAction(capability=capability)
        ):
            return (capability.subject,), (), ()
        case (
            decision_models.AcceptProposalAction(capability=capability)
            | decision_models.MergeProposalAction(capability=capability)
            | decision_models.RejectProposalAction(capability=capability)
            | decision_models.ReturnProposalAction(capability=capability)
        ):
            return (), (), (capability.subject,)
        case decision_models.InspectAction() | decision_models.TransferCoordinatorAction():
            return (), (), ()
        case _ as unreachable:
            assert_never(unreachable)


def _select_worker_attempts(
    state: stored_state.StoredWorkState,
    lease_id: LeaseId | None,
    generation: int,
    now: datetime,
) -> tuple[AttemptId, ...]:
    if lease_id is None:
        return ()
    anchors = {(value.attempt_id, value.generation): value for value in state.authority.attempt_generations}
    return tuple(
        lease.attempt_id
        for lease in state.authority.attempt_leases
        if lease.generation == generation
        and lease.state == authority_models.AttemptLeaseStatus.ACTIVE
        and lease.expires_at > now
        and (anchor := anchors.get((lease.attempt_id, lease.generation))) is not None
        and anchor.lease_id == lease_id
    )


def discover_actions(
    state: stored_state.StoredWorkState,
    role: decision_models.Role,
    *,
    lease_id: LeaseId | None = None,
    generation: int | None = None,
    now: datetime,
) -> DecisionResult[tuple[decision_models.Action, ...]]:
    snapshot = project_decision_snapshot(state, now)
    selected_generation = generation if generation is not None else snapshot.generation
    match role:
        case decision_models.Role.OBSERVER:
            actor = decision_models.ObserverActorAuthority()
        case decision_models.Role.COORDINATOR:
            coordination = state.authority.coordination
            if lease_id is not None:
                if (
                    coordination is None
                    or coordination.state != work_models.CoordinationLeaseStatus.ACTIVE
                    or coordination.lease_id != lease_id
                    or coordination.generation != selected_generation
                    or coordination.expires_at <= now
                ):
                    return DecisionFailure(
                        DecisionFailureCode.COORDINATION_LEASE_REQUIRED,
                        "The coordination lease is not current.",
                    )
                authorization = decision_models.AuthorizationKind.COORDINATION
            else:
                authorization = decision_models.AuthorizationKind.COORDINATOR
            actor = decision_models.ActorAuthority(
                decision_models.Role.COORDINATOR, authorization, selected_generation, lease_id
            )
        case decision_models.Role.WORKER:
            attempts = _select_worker_attempts(state, lease_id, selected_generation, now)
            actor = decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                decision_models.AuthorizationKind.ATTEMPT,
                selected_generation,
                lease_id,
                attempts,
                False,
            )
        case decision_models.Role.PREPARER:
            if lease_id is None:
                preparations = ()
            else:
                anchors = {
                    (value.item_id, value.generation): value for value in state.authority.preparation_generations
                }
                preparations = tuple(
                    lease.item_id
                    for lease in state.authority.preparation_leases
                    if lease.generation == selected_generation
                    and lease.state == authority_models.PreparationLeaseStatus.ACTIVE
                    and lease.expires_at > now
                    and (anchor := anchors.get((lease.item_id, lease.generation))) is not None
                    and anchor.lease_id == lease_id
                )
            actor = decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                decision_models.AuthorizationKind.PREPARATION,
                selected_generation,
                lease_id,
                preparations=preparations,
            )
    return available_actions(snapshot, actor)
