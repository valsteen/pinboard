"""Exact retained work-brief v2 boundary model."""

from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.application import work_brief_models
from pinboard.domain import work_models


class LocalCheckpointV3(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="local",
    tag_field="boundary",
):
    checkpoint_id: work_brief_models.KebabId
    title: work_brief_models.NonEmptyLine
    architecture_impact: work_brief_models.ArchitectureImpact
    outcome_description: work_brief_models.NonEmptyText
    acceptance_criteria: Annotated[tuple[work_brief_models.AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    verification: Annotated[tuple[work_brief_models.VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[work_brief_models.Deferral, ...]


class CrossBoundaryCheckpointV3(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="cross-boundary",
    tag_field="boundary",
):
    checkpoint_id: work_brief_models.KebabId
    title: work_brief_models.NonEmptyLine
    architecture_impact: work_brief_models.ArchitectureImpact
    outcome: Literal["independently-buildable"]
    outcome_description: work_brief_models.NonEmptyText
    contracts: Annotated[tuple[work_brief_models.ContractRecord, ...], msgspec.Meta(min_length=1)]
    acceptance_criteria: Annotated[tuple[work_brief_models.AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    reviewed_authorities: Annotated[tuple[work_brief_models.ReviewedAuthority, ...], msgspec.Meta(min_length=1)]
    coverage: Annotated[tuple[work_brief_models.CoverageRecord, ...], msgspec.Meta(min_length=1)]
    lifecycle_partition: work_brief_models.LifecyclePartition
    verification: Annotated[tuple[work_brief_models.VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[work_brief_models.Deferral, ...]


type WorkBriefCheckpointV3 = LocalCheckpointV3 | CrossBoundaryCheckpointV3


def _current_checkpoint(
    checkpoint: WorkBriefCheckpointV3,
    remaining_work: str,
) -> work_brief_models.WorkBriefCheckpoint:
    disposition = work_brief_models.ContinueCheckpointDisposition(remaining_work)
    match checkpoint:
        case LocalCheckpointV3():
            return work_brief_models.LocalCheckpoint(
                checkpoint.checkpoint_id,
                checkpoint.title,
                checkpoint.architecture_impact,
                checkpoint.outcome_description,
                disposition,
                checkpoint.acceptance_criteria,
                checkpoint.verification,
                checkpoint.deferrals,
            )
        case CrossBoundaryCheckpointV3():
            return work_brief_models.CrossBoundaryCheckpoint(
                checkpoint.checkpoint_id,
                checkpoint.title,
                checkpoint.architecture_impact,
                checkpoint.outcome,
                checkpoint.outcome_description,
                disposition,
                checkpoint.contracts,
                checkpoint.acceptance_criteria,
                checkpoint.reviewed_authorities,
                checkpoint.coverage,
                checkpoint.lifecycle_partition,
                checkpoint.verification,
                checkpoint.deferrals,
            )
        case _ as unreachable:
            assert_never(unreachable)


class _RetainedWorkBriefBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    checkpoint: WorkBriefCheckpointV3
    remaining_work: work_brief_models.NonEmptyText


class WorkBriefV3(_RetainedWorkBriefBase, frozen=True):
    schema: Literal["pinboard-work-brief/v3"]
    checkout_selection: work_models.CheckoutSelection
    obligation_correspondence: Annotated[
        tuple[work_brief_models.ObligationCorrespondence, ...], msgspec.Meta(min_length=1)
    ]

    def __post_init__(self) -> None:
        work_brief_models.WorkBrief(
            "pinboard-work-brief/v4",
            self.artifact_revision,
            self.attempt_id,
            self.item_id,
            self.branch,
            self.base_revision,
            self.owner_task_id,
            self.accepted_scope,
            self.title,
            self.outcome,
            self.supported_production_roots,
            self.product_decision_and_provenance,
            self.testing_strategy,
            self.scope,
            self.bootstrap,
            self.compatibility,
            self.non_goals,
            _current_checkpoint(self.checkpoint, self.remaining_work),
            self.checkout_selection,
            self.obligation_correspondence,
        )


class WorkBriefV2(_RetainedWorkBriefBase, frozen=True):
    schema: Literal["pinboard-work-brief/v2"]


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
