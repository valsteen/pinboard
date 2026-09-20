"""Shared preparation- and attempt-authority use cases.

Native authority tools select exact use cases. This module enriches supplied lease tokens
from current facts, invokes locked application mutation, and reloads authority.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from pinboard.application import ports, query_models, service
from pinboard.application.mutation_models import CommittedEffect
from pinboard.domain import authority_models, work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
)
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId


@dataclass(frozen=True, slots=True)
class AttemptAuthorityMutationResult:
    effect: CommittedEffect
    authority: query_models.AttemptAuthorityStatus


@dataclass(frozen=True, slots=True)
class PreparationAuthorityMutationResult:
    effect: CommittedEffect
    authority: query_models.PreparationAuthorityStatus


def attempt_authority_status(
    store: ports.WorkStore, attempt_id: AttemptId, observed_at: datetime
) -> query_models.AttemptAuthorityStatus | None:
    retained = store.read_attempt_authority_status(attempt_id)
    if (
        retained is not None
        and retained.status == authority_models.AttemptLeaseStatus.ACTIVE
        and retained.expires_at <= observed_at
    ):
        return replace(retained, status=authority_models.AttemptLeaseStatus.EXPIRED)
    return retained


def preparation_authority_status(
    store: ports.WorkStore, item_id: ItemId, observed_at: datetime
) -> query_models.PreparationAuthorityStatus | None:
    retained = store.read_preparation_authority_status(item_id)
    if (
        retained is not None
        and retained.status == authority_models.PreparationLeaseStatus.ACTIVE
        and retained.expires_at <= observed_at
    ):
        return replace(retained, status=authority_models.PreparationLeaseStatus.EXPIRED)
    return retained


def _committed_attempt_result(
    store: ports.WorkStore,
    attempt_id: AttemptId,
    result: DecisionResult[CommittedEffect],
) -> DecisionResult[AttemptAuthorityMutationResult]:
    if isinstance(result, DecisionFailure):
        return result
    retained = store.read_attempt_authority_status(attempt_id)
    if retained is None:
        raise RuntimeError("Committed attempt authority did not reload.")
    return AttemptAuthorityMutationResult(result, retained)


def acquire_attempt_authority(
    store: ports.WorkStore,
    *,
    attempt_id: AttemptId,
    task_id: TaskId,
    host_id: HostId,
    lease_id: LeaseId,
    acquired_at: datetime,
    expires_at: datetime,
) -> DecisionResult[AttemptAuthorityMutationResult]:
    return _committed_attempt_result(
        store,
        attempt_id,
        service.acquire_attempt_authority(
            store,
            attempt_id=attempt_id,
            task_id=task_id,
            host_id=host_id,
            lease_id=lease_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        ),
    )


def _supplied_attempt_authority(
    store: ports.WorkStore,
    attempt_id: AttemptId,
    lease_id: LeaseId,
    generation: int,
    operation_time: datetime,
) -> work_models.CommandAttemptAuthority | None:
    snapshot = store.read_decision_facts(
        query_models.DecisionScope((), (), (), (), (attempt_id,), (), (), ()), operation_time
    ).snapshot
    current = snapshot.command_attempt_authority(attempt_id)
    return None if current is None else replace(current, lease_id=lease_id, generation=generation)


def renew_attempt_authority(
    store: ports.WorkStore,
    *,
    attempt_id: AttemptId,
    lease_id: LeaseId,
    generation: int,
    renewed_at: datetime,
    expires_at: datetime,
) -> DecisionResult[AttemptAuthorityMutationResult]:
    supplied = _supplied_attempt_authority(store, attempt_id, lease_id, generation, renewed_at)
    if supplied is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.", None)
    return _committed_attempt_result(
        store,
        attempt_id,
        service.decide_and_commit_attempt_authority_change(
            store, authority_models.RenewAttemptAuthority(supplied, renewed_at, expires_at)
        ),
    )


def release_attempt_authority(
    store: ports.WorkStore,
    *,
    attempt_id: AttemptId,
    lease_id: LeaseId,
    generation: int,
    released_at: datetime,
) -> DecisionResult[AttemptAuthorityMutationResult]:
    supplied = _supplied_attempt_authority(store, attempt_id, lease_id, generation, released_at)
    if supplied is None:
        return DecisionFailure(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, "Attempt authority is not active.", None)
    return _committed_attempt_result(
        store,
        attempt_id,
        service.decide_and_commit_attempt_authority_change(
            store, authority_models.ReleaseAttemptAuthority(supplied, released_at)
        ),
    )


def revoke_attempt_authority(
    store: ports.WorkStore,
    *,
    attempt_id: AttemptId,
    lease_id: LeaseId,
    generation: int,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    revoked_at: datetime,
) -> DecisionResult[AttemptAuthorityMutationResult]:
    requested = authority_models.RevokeAttemptAuthority(
        attempt_id,
        lease_id,
        generation,
        actor_task_id,
        actor_host_id,
        revoked_at,
    )
    return _committed_attempt_result(
        store,
        attempt_id,
        service.decide_and_commit_attempt_authority_change(store, requested),
    )


def _committed_preparation_result(
    store: ports.WorkStore,
    item_id: ItemId,
    result: DecisionResult[CommittedEffect],
) -> DecisionResult[PreparationAuthorityMutationResult]:
    if isinstance(result, DecisionFailure):
        return result
    retained = store.read_preparation_authority_status(item_id)
    if retained is None:
        raise RuntimeError("Committed preparation authority did not reload.")
    return PreparationAuthorityMutationResult(result, retained)


def start_preparation_authority(
    store: ports.WorkStore,
    *,
    item_id: ItemId,
    task_id: TaskId,
    host_id: HostId,
    lease_id: LeaseId,
    acquired_at: datetime,
    expires_at: datetime,
) -> DecisionResult[PreparationAuthorityMutationResult]:
    result = service.start_preparation(
        store,
        item_id=item_id,
        task_id=task_id,
        host_id=host_id,
        lease_id=lease_id,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )
    if isinstance(result, DecisionFailure):
        return result
    retained = store.read_preparation_authority_status(item_id)
    if retained is None:
        raise RuntimeError("Committed preparation authority did not reload.")
    return PreparationAuthorityMutationResult(result.effect, retained)


def _supplied_preparation_authority(
    store: ports.WorkStore,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    operation_time: datetime,
) -> work_models.PreparationCommandAuthority | None:
    snapshot = store.read_decision_facts(
        query_models.DecisionScope((item_id,), (), (), (), (), (), (), ()), operation_time
    ).snapshot
    current = snapshot.command_preparation_authority(item_id)
    return None if current is None else replace(current, lease_id=lease_id, generation=generation)


def renew_preparation_authority(
    store: ports.WorkStore,
    *,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    renewed_at: datetime,
    expires_at: datetime,
) -> DecisionResult[PreparationAuthorityMutationResult]:
    supplied = _supplied_preparation_authority(store, item_id, lease_id, generation, renewed_at)
    if supplied is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority is not active.", None)
    return _committed_preparation_result(
        store,
        item_id,
        service.decide_and_commit_preparation_authority_change(
            store, authority_models.RenewPreparationAuthority(supplied, renewed_at, expires_at)
        ),
    )


def release_preparation_authority(
    store: ports.WorkStore,
    *,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    released_at: datetime,
) -> DecisionResult[PreparationAuthorityMutationResult]:
    supplied = _supplied_preparation_authority(store, item_id, lease_id, generation, released_at)
    if supplied is None:
        return DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, "Preparation authority is not active.", None)
    return _committed_preparation_result(
        store,
        item_id,
        service.decide_and_commit_preparation_authority_change(
            store, authority_models.ReleasePreparationAuthority(supplied, released_at)
        ),
    )


def revoke_preparation_authority(
    store: ports.WorkStore,
    *,
    item_id: ItemId,
    lease_id: LeaseId,
    generation: int,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    revoked_at: datetime,
) -> DecisionResult[PreparationAuthorityMutationResult]:
    requested = authority_models.RevokePreparationAuthority(
        item_id,
        lease_id,
        generation,
        actor_task_id,
        actor_host_id,
        revoked_at,
    )
    return _committed_preparation_result(
        store,
        item_id,
        service.decide_and_commit_preparation_authority_change(store, requested),
    )
