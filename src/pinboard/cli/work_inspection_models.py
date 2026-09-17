"""Installed read-only work-inspection presentation records."""

from typing import Literal

import msgspec

from pinboard.adapters.review_operations import PriorCheckpointPackageSelection, ReviewRound
from pinboard.application import dispatch_models, query_models
from pinboard.application.action_models import ProjectedActionView as ActionView


class NoCandidateRecovery(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class CandidateRecovery(msgspec.Struct, tag="present", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    candidate_kind: Literal["working-tree", "commit"]
    candidate: str
    branch: str
    preimage_revision: str
    artifact_ref_id: int
    selector: str
    sha256: str
    size_bytes: int
    restore_command: tuple[str, ...]


type CandidateRecoverySelection = NoCandidateRecovery | CandidateRecovery


class CandidateRestoreView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-candidate-restore/v1"]
    attempt_id: str
    candidate: str
    changed: bool
    source_checkout: str


class AttemptView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    continuation: query_models.AttemptContinuation
    candidate_recovery: CandidateRecoverySelection


class VerifiedArtifactReferenceView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-verified-artifact-reference/v1"]
    artifact_ref_id: int
    selector: str
    sha256: str
    size_bytes: int
    accepted_revision: int
    verified: Literal[True]


class TransitionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: str
    committed_revision: str
    history_id: int
    continuation: query_models.AttemptContinuation | None


class ReviewJobView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-review-job/v4"]
    attempt_id: str
    candidate_revision: str
    candidate_recovery: CandidateRecovery
    owner_task_id: str
    brief_path: str
    brief_sha256: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    result_path: str
    result_sha256: str
    prior_checkpoint_package: PriorCheckpointPackageSelection
    review_round: ReviewRound
    prompt_reference: dispatch_models.PromptReferenceView
    native_launch: dispatch_models.NativeLaunchEnvelope
    changed_surfaces: tuple[str, ...]
    return_contract: str


class StatusView(msgspec.Struct, frozen=True):
    stored_state_opened: bool = msgspec.field(name="valid")
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    active_attempts: tuple[str, ...]
    counts: dict[str, int]
    intake_item_count: int
    authority: str = "v2"


class CompletionInspectionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: str
    kind: Literal["inspect-completion"]
    subject: str
    label: str
    effect: Literal["advisory"]
    inspection_arguments: tuple[str, ...]


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView | CompletionInspectionView, ...]
