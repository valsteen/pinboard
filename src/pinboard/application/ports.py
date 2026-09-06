from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from pinboard.application import stored_state
from pinboard.application.artifacts import ArtifactRef
from pinboard.application.mutation_models import CommittedEffect, MutationReceipt, StoredStateMutation
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId, ProposalId


class WorkTransaction(AbstractContextManager["WorkTransaction"], Protocol):
    def decision_state(
        self,
        selected_artifact_ref_ids: tuple[ArtifactRefId, ...] = (),
        *,
        subject_item_ids: tuple[ItemId, ...] = (),
        subject_attempt_ids: tuple[AttemptId, ...] = (),
        subject_proposal_ids: tuple[ProposalId, ...] = (),
    ) -> stored_state.StoredWorkState: ...

    def coordination_state(self) -> stored_state.StoredWorkState: ...

    def attempt_authority_state(self, attempt_id: AttemptId) -> stored_state.StoredWorkState: ...

    def commit(
        self, state: stored_state.StoredWorkState, mutation: StoredStateMutation
    ) -> DecisionResult[MutationReceipt]: ...

    def commit_with_effect(
        self, state: stored_state.StoredWorkState, mutation: StoredStateMutation
    ) -> DecisionResult[CommittedEffect]: ...


class WorkStore(Protocol):
    # jscpd:ignore-start
    # Store and transaction boundaries deliberately expose the same exact read scope.
    def decision_state(
        self,
        selected_artifact_ref_ids: tuple[ArtifactRefId, ...] = (),
        *,
        subject_item_ids: tuple[ItemId, ...] = (),
        subject_attempt_ids: tuple[AttemptId, ...] = (),
        subject_proposal_ids: tuple[ProposalId, ...] = (),
    ) -> stored_state.StoredWorkState: ...

    # jscpd:ignore-end

    def write(self) -> WorkTransaction: ...

    def artifact_reference(
        self, kind: work_models.ArtifactKind, key: str, revision: int
    ) -> stored_state.ArtifactReference | None: ...

    def accept_artifact_reference(
        self,
        published: ArtifactRef,
        accepted_at: datetime,
    ) -> DecisionResult[stored_state.ArtifactReference]: ...
