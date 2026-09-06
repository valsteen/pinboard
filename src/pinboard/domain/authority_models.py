from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId


class AttemptLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PreparationLeaseStatus(Enum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PreparationLeaseAuthority:
    host_epoch: int
    item: ItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: PreparationLeaseStatus


@dataclass(frozen=True, slots=True)
class InactivePreparationAuthority:
    host_epoch: int
    item: ItemId
    definition_revision: int
    definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime
    state: PreparationLeaseStatus


@dataclass(frozen=True, slots=True)
class AcquireInitialPreparationAuthority:
    host_epoch: int
    item: ItemId
    expected_project_revision: str
    expected_item_subject_revision: str
    expected_definition_revision: int
    expected_definition_digest: str
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransferPreparationAuthority:
    current: InactivePreparationAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewPreparationAuthority:
    current: work_models.PreparationCommandAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleasePreparationAuthority:
    current: work_models.PreparationCommandAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokePreparationAuthority:
    item: ItemId
    lease_id: LeaseId
    generation: int
    task_id: TaskId
    host_id: HostId
    revoked_at: datetime


type PreparationAuthorityOperation = (
    AcquireInitialPreparationAuthority
    | TransferPreparationAuthority
    | RenewPreparationAuthority
    | ReleasePreparationAuthority
    | RevokePreparationAuthority
)


@dataclass(frozen=True, slots=True)
class PreparationAuthorityDecision:
    item: ItemId
    counter_before: int
    counter_after: int
    expected_retained: PreparationLeaseAuthority | None
    proposed_replacement: PreparationLeaseAuthority


@dataclass(frozen=True, slots=True)
class AttemptLeaseAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    acquired_at: datetime
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class InactiveAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    expires_at: datetime
    state: AttemptLeaseStatus


@dataclass(frozen=True, slots=True)
class AcquireInitialAttemptAuthority:
    host_epoch: int
    attempt: AttemptId
    item: ItemId
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TransferAttemptAuthority:
    current: InactiveAttemptAuthority
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RenewAttemptAuthority:
    current: work_models.CommandAttemptAuthority
    renewed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseAttemptAuthority:
    current: work_models.CommandAttemptAuthority
    released_at: datetime


@dataclass(frozen=True, slots=True)
class RevokeAttemptAuthority:
    attempt: AttemptId
    lease_id: LeaseId
    generation: int
    task_id: TaskId
    host_id: HostId
    revoked_at: datetime


type AttemptAuthorityOperation = (
    AcquireInitialAttemptAuthority
    | TransferAttemptAuthority
    | RenewAttemptAuthority
    | ReleaseAttemptAuthority
    | RevokeAttemptAuthority
)


@dataclass(frozen=True, slots=True)
class AttemptAuthorityDecision:
    attempt: AttemptId
    counter_before: int
    counter_after: int
    expected_retained: AttemptLeaseAuthority | None
    proposed_replacement: AttemptLeaseAuthority
