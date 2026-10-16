"""Commit one initiative change through a supplied transaction; read facts without lifecycle inference."""

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from pinboard.application.mutation_models import MutationAllocation
from pinboard.domain import initiatives as models
from pinboard.domain.identifiers import HostId, TaskId


class InitiativeTransaction(Protocol):
    def read_mutation_allocation(self) -> MutationAllocation: ...

    def read_initiative_facts(self, initiative_id: models.InitiativeId) -> models.CurrentFacts | None: ...

    def read_membership_condition(
        self, initiative_id: models.InitiativeId, change: models.AddMember | models.RemoveMember
    ) -> tuple[bool, bool]: ...

    def commit_initiative(
        self,
        initiative_id: models.InitiativeId,
        change: models.Change,
        accepted: models.Accepted,
        provenance: models.Provenance,
    ) -> int: ...


class InitiativeStore(Protocol):
    def write(self) -> AbstractContextManager[InitiativeTransaction]: ...

    def read_initiative(self, initiative_id: models.InitiativeId) -> models.Initiative | None: ...


def change(
    store: InitiativeStore,
    initiative_id: models.InitiativeId,
    requested: models.Change,
    reason: models.HumanReason,
    actor_task_id: TaskId,
    actor_host_id: HostId,
    committed_at: datetime,
) -> int | models.Rejected:
    with store.write() as transaction:
        current = transaction.read_initiative_facts(initiative_id)
        item_exists, active = (
            transaction.read_membership_condition(initiative_id, requested)
            if isinstance(requested, (models.AddMember, models.RemoveMember))
            else (False, False)
        )
        accepted = models.decide(initiative_id, current, requested, item_exists, active)
        if isinstance(accepted, models.Rejected):
            return accepted
        return transaction.commit_initiative(
            initiative_id,
            requested,
            accepted,
            models.Provenance(
                reason,
                actor_task_id,
                actor_host_id,
                committed_at,
                transaction.read_mutation_allocation().project_revision + 1,
            ),
        )
