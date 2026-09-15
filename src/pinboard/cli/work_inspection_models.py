"""Installed read-only work-inspection presentation records."""

from typing import Literal

import msgspec

from pinboard.application import action_models, dispatch_models, query_models, work_briefs
from pinboard.application.action_models import InputContractView
from pinboard.application.action_models import ProjectedActionView as ActionView
from pinboard.domain import decision_models


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


class CompletionPackageView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: int
    package_sha256: str
    artifact_ref_id: int
    selector: str
    size_bytes: int


class CompletionInputContractView(InputContractView, frozen=True, forbid_unknown_fields=True):
    candidate: str | None
    checkpoint_packages: tuple[CompletionPackageView, ...]

    def __post_init__(self) -> None:
        if self.action_kind != decision_models.ActionKind.COMPLETE:
            raise ValueError("completion input contracts require the complete action kind")
        expected = decision_models.action_semantics(self.action_kind)
        if self.semantics != action_models.ActionSemanticsView(
            expected.use_case,
            expected.lifecycle_effect,
            expected.permitted_roles,
            expected.subject_kind,
            expected.lifecycle_precondition,
            expected.practical_result,
        ):
            raise ValueError("completion input contract semantics must match complete")
        expected_model = (
            action_models.CoveredCompleteInputPayload
            if self.checkpoint_packages
            else action_models.EvidenceInputPayload
        )
        if msgspec.json.encode(self.payload_schema, order="sorted") != msgspec.json.encode(
            msgspec.json.schema(expected_model), order="sorted"
        ):
            raise ValueError("completion payload schema must be one exact complete-action leaf")


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
