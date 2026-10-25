"""Exact displayed cancellation facts and the receipt that retains human confirmation."""

from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal

import msgspec

from pinboard.application import stored_state
from pinboard.domain import work_models
from pinboard.domain.history import RecordText
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, LeaseId, TaskId, WorkItemId

type Digest = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]


class CancellationAuthority(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    task_id: TaskId
    host_id: HostId
    lease_id: LeaseId
    generation: int
    status: Annotated[str, msgspec.Meta(pattern=r"\A(?:active|expired|released|revoked)\z")]
    expires_at: datetime
    acquired_at: datetime


class CancellationPreparation(CancellationAuthority, frozen=True, forbid_unknown_fields=True):
    definition_revision: int
    definition_digest: Digest


class CancellationAttempt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    state: work_models.AttemptState
    subject_revision: int
    branch: str
    base_revision: str
    accepted_scope_revision: int
    accepted_scope_digest: Digest
    brief_artifact_ref_id: ArtifactRefId
    result_artifact_ref_id: ArtifactRefId | None
    candidate: str | None
    authority: CancellationAuthority | None


class CancellationRelatedItem(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    title: str
    state: stored_state.StoredWorkItemState
    subject_revision: int
    definition_revision: int
    definition_digest: Digest


# Retained v1 receipts bind this shared preview literally; remove when none require the historical value.
type CompatibilityCancellationDependencyEffectV1 = Literal[
    "Dropped prerequisites remain unsatisfied; dependent commitments are retained."
]


class CancellationPreview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cancellation-preview/v1"]
    item_id: WorkItemId
    title: str
    state: stored_state.StoredWorkItemState
    subject_revision: int
    definition_revision: int
    definition_digest: Digest
    objective: str
    scope: tuple[str, ...]
    attempt: CancellationAttempt | None
    preparation: CancellationPreparation | None
    prerequisites: tuple[CancellationRelatedItem, ...]
    dependents: tuple[CancellationRelatedItem, ...]
    outcome: Literal["dropped"]
    retained: Literal[
        "Definitions, attempts, history, reviews, candidate evidence, code, branches and worktrees are retained."
    ]
    dependency_effect: (
        Literal[
            "Whole-item dependencies on dropped items remain unsatisfied; matching accepted checkpoints remain satisfied; dependent commitments are retained."
        ]
        | CompatibilityCancellationDependencyEffectV1
    )
    process_effect: Literal[
        "Pinboard authority is fenced; local processes are not stopped. Stop or await your known worker before confirming."
    ]


class CancellationBinding(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    work_root: Annotated[str, msgspec.Meta(min_length=1)]
    schema: Literal["pinboard-cancellation-binding/v1"]
    item_id: WorkItemId
    preview_sha256: Digest


class CancellationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-cancellation/v1"]
    binding: CancellationBinding
    preview: CancellationPreview
    human_confirmation: RecordText

    def __post_init__(self) -> None:

        if not self.human_confirmation.strip():
            raise ValueError("cancellation requires nonempty human confirmation")
        if (
            self.binding.item_id != self.preview.item_id
            or self.binding.preview_sha256 != sha256(msgspec.json.encode(self.preview, order="sorted")).hexdigest()
        ):
            raise ValueError("cancellation binding differs from its recorded preview")
