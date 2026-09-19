"""Exact retained work-brief v2 boundary model."""

from typing import Annotated, Literal

import msgspec

from pinboard.application import work_brief_models


class WorkBriefV2(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief/v2"]
    artifact_revision: work_brief_models.PositiveInt
    attempt_id: work_brief_models.KebabId
    item_id: work_brief_models.KebabId
    branch: work_brief_models.NonEmptyLine
    base_revision: work_brief_models.NonEmptyLine
    owner_task_id: work_brief_models.NonEmptyLine
    accepted_scope: work_brief_models.AcceptedScope
    title: work_brief_models.NonEmptyLine
    outcome: work_brief_models.NonEmptyText
    supported_production_roots: work_brief_models.NonEmptyTexts
    product_decision_and_provenance: work_brief_models.NonEmptyText
    testing_strategy: work_brief_models.NonEmptyText
    scope: work_brief_models.NonEmptyTexts
    bootstrap: tuple[work_brief_models.NonEmptyText, ...]
    compatibility: tuple[work_brief_models.NonEmptyText, ...]
    non_goals: tuple[work_brief_models.NonEmptyText, ...]
    checkpoint: work_brief_models.WorkBriefCheckpoint
    remaining_work: work_brief_models.NonEmptyText


class WorkBriefReviewV2(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Exact retained ready-review v2 evidence for retained brief v2 packages."""

    schema: Literal["pinboard-work-brief-review/v2"]
    attempt_id: work_brief_models.KebabId
    checkpoint_id: work_brief_models.KebabId
    checkpoint_sha256: work_brief_models.Sha256
    reviewed_authority_set_sha256: work_brief_models.Sha256
    reviewer_task_id: work_brief_models.NonEmptyLine
    status: Literal["complete"]
    verdict: Literal["ready"]
    coverage: Annotated[tuple[work_brief_models.ReviewCoverageResult, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        coverage_keys = tuple((record.authority_id, record.family) for record in self.coverage)
        if len(set(coverage_keys)) != len(coverage_keys):
            raise ValueError("Brief review coverage must identify every authority family at most once.")
