from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pinboard.application import query_models, stored_state
from pinboard.application.artifacts import ArtifactRef, EvidenceArtifactRef, ResultArtifactRef
from pinboard.application.handover import HandoverState
from pinboard.application.mutation_models import (
    CheckpointMutationAllocation,
    CommittedEffect,
    MutationAllocation,
    StoredStateMutation,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HistoryId, ItemId, LeaseId
from pinboard.domain.ledger import LedgerSnapshot


class WorkStoreError(RuntimeError):
    """Infrastructure failure owned by the work-store port."""


@dataclass(frozen=True, slots=True)
class ArtifactReferenceAcceptance:
    reference: stored_state.ArtifactReference
    ledger_changed: bool


class WorkTransaction(Protocol):
    def read_decision_facts(self, scope: query_models.DecisionScope, now: datetime) -> query_models.DecisionFacts: ...

    def read_mutation_allocation(self) -> MutationAllocation: ...

    def read_checkpoint_mutation_allocation(
        self, artifacts: tuple[ArtifactRef | ResultArtifactRef | EvidenceArtifactRef, ...]
    ) -> CheckpointMutationAllocation: ...

    def read_live_item_count(self) -> int: ...

    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None: ...

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None: ...

    def read_attempt_generation(self, attempt_id: AttemptId) -> int: ...

    def read_preparation_generation(self, item_id: ItemId) -> int: ...

    def commit(self, mutation: StoredStateMutation) -> DecisionResult[CommittedEffect]: ...


class WorkStore(Protocol):
    def write(self) -> AbstractContextManager[WorkTransaction]: ...

    def accept_artifact_reference(
        self,
        work_root: Path,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[ArtifactReferenceAcceptance]: ...

    def read_artifact_reference(
        self, kind: work_models.ArtifactKind, key: str, revision: int
    ) -> stored_state.ArtifactReference | None: ...

    def read_artifact_reference_by_id(
        self, artifact_ref_id: ArtifactRefId
    ) -> stored_state.ArtifactReference | None: ...

    def read_attempt_context(self, attempt_id: AttemptId) -> query_models.AttemptContextFacts | None: ...

    def read_review_job_context(
        self,
        attempt_id: AttemptId,
        checkpoint_history_id: HistoryId | None,
        correction_history_id: HistoryId | None,
    ) -> query_models.ReviewJobContextFacts | None: ...

    def read_decision_facts(self, scope: query_models.DecisionScope, now: datetime) -> query_models.DecisionFacts: ...

    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None: ...

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None: ...

    def read_item_definition(self, item_id: ItemId) -> query_models.ItemDefinitionFacts: ...

    def read_item_definition_history(
        self, item_id: ItemId, *, limit: int, before_revision: int | None
    ) -> query_models.ItemDefinitionHistoryFacts: ...

    def read_item_status(self, item_id: ItemId) -> query_models.ItemStatusFacts | None: ...

    def read_parallel_preview(self, item_ids: tuple[ItemId, ...]) -> query_models.ParallelPreviewFacts | None: ...

    def read_project_status(self) -> query_models.ProjectStatusFacts: ...

    def read_current_action_snapshot(self, now: datetime) -> LedgerSnapshot: ...

    def read_leased_action_snapshot(
        self, role: decision_models.Role, lease_id: LeaseId, generation: int, now: datetime
    ) -> LedgerSnapshot: ...

    def read_current_parallel_snapshot(self, now: datetime) -> LedgerSnapshot: ...

    def read_project_overview(self, now: datetime) -> query_models.ProjectOverviewFacts: ...

    def read_generated_view_facts(
        self,
        item_ids: tuple[ItemId, ...],
        attempt_ids: tuple[AttemptId, ...],
        history_ids: tuple[HistoryId, ...],
        now: datetime,
    ) -> query_models.GeneratedViewFacts: ...


class HandoverReader(Protocol):
    def read_handover_batches(self) -> Iterable[HandoverState]: ...


class ValidatedStateReader(Protocol):
    """Explicit capability for full integrity validation and state assembly."""

    def validated_snapshot(self) -> stored_state.StoredWorkState: ...


class AuthorityStatusReader(Protocol):
    def read_attempt_authority_status(self, attempt_id: AttemptId) -> query_models.AttemptAuthorityStatus | None: ...

    def read_preparation_authority_status(self, item_id: ItemId) -> query_models.PreparationAuthorityStatus | None: ...


class ItemDefinitionReader(Protocol):
    def read_item_definition(self, item_id: ItemId) -> query_models.ItemDefinitionFacts: ...

    def read_item_definition_history(
        self, item_id: ItemId, *, limit: int, before_revision: int | None
    ) -> query_models.ItemDefinitionHistoryFacts: ...


class ItemStatusReader(Protocol):
    def read_item_status(self, item_id: ItemId) -> query_models.ItemStatusFacts | None: ...


class AttemptContextReader(Protocol):
    def read_attempt_context(self, attempt_id: AttemptId) -> query_models.AttemptContextFacts | None: ...


class ReviewJobContextReader(Protocol):
    def read_review_job_context(
        self,
        attempt_id: AttemptId,
        checkpoint_history_id: HistoryId | None,
        correction_history_id: HistoryId | None,
    ) -> query_models.ReviewJobContextFacts | None: ...


class ParallelPreviewReader(Protocol):
    def read_parallel_preview(self, item_ids: tuple[ItemId, ...]) -> query_models.ParallelPreviewFacts | None: ...


class GeneratedViewReader(Protocol):
    def read_generated_view_facts(
        self,
        item_ids: tuple[ItemId, ...],
        attempt_ids: tuple[AttemptId, ...],
        history_ids: tuple[HistoryId, ...],
        now: datetime,
    ) -> query_models.GeneratedViewFacts: ...


class GeneratedViewSetReader(Protocol):
    """Project-wide generated-view facts without unrelated stored state."""

    def read_all_generated_view_facts(self, now: datetime) -> query_models.GeneratedViewFacts: ...
