"""Discover currently legal actions from one already-loaded stored snapshot.

The caller owns SQLite access and time sampling. This module projects the
snapshot into domain decision facts and asks the domain for legal actions.
"""

from datetime import datetime

from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.domain import authority_models, decision_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import AttemptId, LeaseId


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
    selected_generation = generation if generation is not None else 0
    match role:
        case decision_models.Role.OBSERVER:
            actor = decision_models.ObserverActorAuthority()
        case decision_models.Role.PROJECT:
            actor = decision_models.ActorAuthority(
                decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
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
