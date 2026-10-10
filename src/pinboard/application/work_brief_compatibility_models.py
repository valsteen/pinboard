"""Strict original brief, review and completion facts for historical consumers only."""

from typing import Annotated, Literal

import msgspec

from pinboard.application import action_models, work_brief_models
from pinboard.domain import work_models


class HistoricalCoveredCompletionInput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Exact original receipt facts, retained while completion history requires them."""

    schema: Literal["pinboard-covered-completion/v1"]
    candidate: action_models.NonEmptyLine
    evidence: action_models.NonEmptyLine
    reviewer_task_id: action_models.NonEmptyLine
    result_sha256: action_models.Sha256
    review_sha256: action_models.Sha256
    packages: Annotated[tuple[action_models.CoveredCompletionPackageInputPayload, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        history_ids = tuple(row.history_id for row in self.packages)
        if history_ids != tuple(sorted(set(history_ids))):
            raise ValueError("packages must be unique and strictly ascending by history_id")


class HistoricalCompletionReviewPackageV1(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Original package facts for archive, validation and export; never execution.

    Remove only when no supported retained completion consumer requires them.
    """

    schema: Literal["pinboard-completion-review-package/v1"]
    attempt_id: work_brief_models.KebabId
    item_id: work_brief_models.KebabId
    candidate: work_brief_models.NonEmptyLine
    outcome_evidence: work_brief_models.NonEmptyLine
    reviewer_task_id: work_brief_models.NonEmptyLine
    accepted_scope: work_brief_models.AcceptedScope
    accepted_brief: work_brief_models.AcceptedBriefCompletionIdentity
    terminal_result: work_brief_models.TerminalResultCompletionIdentity
    final_review: work_brief_models.FinalReviewCompletionIdentity
    checkpoint_coverage: Annotated[
        tuple[work_brief_models.CompletionCheckpointCoverage, ...], msgspec.Meta(min_length=1)
    ]

    def __post_init__(self) -> None:
        history_ids = tuple(row.history_id for row in self.checkpoint_coverage)
        if history_ids != tuple(sorted(set(history_ids))):
            raise ValueError("checkpoint_coverage must be unique and strictly ascending by history_id")


type HistoricalCompletionReviewPackage = work_brief_models.CompletionReviewPackage | HistoricalCompletionReviewPackageV1


def canonical_historical_completion_review_package_bytes(package: HistoricalCompletionReviewPackageV1) -> bytes:
    return msgspec.json.encode(package, order="sorted") + b"\n"


class HistoricalLocalCheckpoint(
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


class HistoricalCrossBoundaryCheckpoint(
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


type HistoricalWorkBriefCheckpoint = HistoricalLocalCheckpoint | HistoricalCrossBoundaryCheckpoint


class _HistoricalWorkBriefBase(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
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
    checkpoint: HistoricalWorkBriefCheckpoint
    remaining_work: work_brief_models.NonEmptyText


class HistoricalWorkBriefV3(_HistoricalWorkBriefBase, frozen=True):
    """Exact original facts, never current execution, status or recovery authority.

    Retain while supported archive, package or readable-history consumers require them.
    """

    schema: Literal["pinboard-work-brief/v3"]
    checkout_selection: work_models.CheckoutSelection
    obligation_correspondence: Annotated[
        tuple[work_brief_models.ObligationCorrespondence, ...], msgspec.Meta(min_length=1)
    ]

    def __post_init__(self) -> None:
        work_brief_models.validate_work_brief_relations(
            self,
            self.checkpoint if isinstance(self.checkpoint, HistoricalCrossBoundaryCheckpoint) else None,
            self.obligation_correspondence,
        )


class HistoricalWorkBriefV2(_HistoricalWorkBriefBase, frozen=True):
    """Original full-field facts for archive and package closure, never execution.

    Retain while original brief bytes have supported historical consumers.
    """

    schema: Literal["pinboard-work-brief/v2"]


class HistoricalWorkBriefReviewV2(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Original nine-field facts, never current readiness or recovery authority.

    Retain while supported historical package closure requires original reviews.
    """

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
        work_brief_models.validate_review_coverage(self.coverage)


def decode_canonical_historical_work_brief_review(
    data: bytes,
) -> work_brief_models.WorkBriefResult[HistoricalWorkBriefReviewV2]:
    """Read only exact original ready-review facts for historical package closure."""
    try:
        review = msgspec.json.decode(data, type=HistoricalWorkBriefReviewV2)
    except (msgspec.DecodeError, ValueError) as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode historical work brief review facts: {error}",
        )
    if data != msgspec.json.encode(review, order="sorted") + b"\n":
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Historical work brief review bytes are not the canonical msgspec encoding.",
        )
    return review
