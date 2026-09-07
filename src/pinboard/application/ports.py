from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import MutationReceipt, StoredStateMutation
from pinboard.domain.errors import DecisionResult


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
