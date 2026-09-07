from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import query_models, stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import MutationReceipt, StoredStateMutation
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import AttemptId, ItemId


class WorkStoreError(RuntimeError):
    """Infrastructure failure owned by the work-store port."""


@dataclass(frozen=True, slots=True)
class ArtifactReferenceAcceptance:
    reference: stored_state.ArtifactReference
    ledger_changed: bool


class WorkTransaction(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[MutationReceipt]: ...


class WorkStore(Protocol):
    def snapshot(self) -> stored_state.StoredWorkState: ...

    def write(self) -> AbstractContextManager[WorkTransaction]: ...

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[ArtifactReferenceAcceptance]: ...


class AuthorityStatusReader(Protocol):
    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None: ...

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None: ...
