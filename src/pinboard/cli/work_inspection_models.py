"""Installed read-only work-inspection presentation records."""

from typing import Literal

import msgspec

from pinboard.application import dispatch_models, query_models
from pinboard.cli import work_briefs


class AttemptView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    continuation: query_models.AttemptContinuation


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


class NoPriorCheckpointPackage(msgspec.Struct, tag="absent", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class PriorCheckpointPackage(msgspec.Struct, tag="present", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    history_id: int
    artifact_ref_id: int
    path: str
    sha256: str
    package: work_briefs.CheckpointPackage
    candidate_artifact_ref_id: int
    candidate_path: str
    candidate_sha256: str
    candidate_size_bytes: int


type PriorCheckpointPackageSelection = NoPriorCheckpointPackage | PriorCheckpointPackage


class InitialReviewRound(msgspec.Struct, tag="initial", tag_field="kind", frozen=True, forbid_unknown_fields=True):
    pass


class CorrectionReviewRound(
    msgspec.Struct, tag="correction", tag_field="kind", frozen=True, forbid_unknown_fields=True
):
    history_id: int
    candidate_revision: str
    reason: str
    review_path: str
    review_sha256: str


type ReviewRound = InitialReviewRound | CorrectionReviewRound


class ReviewJobView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-review-job/v4"]
    attempt_id: str
    candidate_revision: str
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


class ActionSemanticsView(msgspec.Struct, frozen=True):
    use_case: str
    effect: str
    permitted_roles: tuple[str, ...]
    subject_kind: str
    lifecycle_precondition: str
    practical_result: str


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    semantics: ActionSemanticsView
    payload_schema: msgspec.Raw | None


class ActionView(msgspec.Struct, frozen=True, omit_defaults=True):
    action_id: str
    kind: str
    subject: str
    label: str
    subject_revision: str | None
    authorization: str
    lease_id: str | None
    generation: int | None
    semantics: ActionSemanticsView
    input_contract: InputContractView | None = None


class CompletionPackageView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    package_sha256: str
    artifact_ref_id: int
    selector: str
    size_bytes: int


class CompletionInputContractView(InputContractView, frozen=True, forbid_unknown_fields=True):
    candidate: str | None
    checkpoint_packages: tuple[CompletionPackageView, ...]


class CompletionInspectionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: str
    kind: Literal["inspect-completion"]
    subject: str
    label: str
    effect: Literal["advisory"]
    inspection_arguments: tuple[str, ...]


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView | CompletionInspectionView, ...]


class ParallelItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    outcome: str
    reasons: tuple[query_models.ParallelReason, ...]


class ParallelPreviewView(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: str
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]
