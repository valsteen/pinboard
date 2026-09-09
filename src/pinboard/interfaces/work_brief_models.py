import re
from dataclasses import dataclass
from typing import Annotated, Literal, assert_never

import msgspec

from pinboard.interfaces.brief_source_models import parse_authority_selector
from pinboard.interfaces.errors import BriefSourceFailure

type NonEmptyText = Annotated[str, msgspec.Meta(min_length=1)]
type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A\S(?:[^\n]*\S)?\z")]
type KebabId = Annotated[str, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type NonNegativeInt = Annotated[int, msgspec.Meta(ge=0)]
type NonEmptyTexts = Annotated[tuple[NonEmptyText, ...], msgspec.Meta(min_length=1)]

PROHIBITION: re.Pattern[str] = re.compile(
    r"\b(?:must not|do not|cannot|never|prohibition|prohibited)\b",
    re.IGNORECASE,
)


class AcceptedScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: PositiveInt
    digest: Sha256


class NoArchitectureImpact(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="none",
    tag_field="kind",
):
    reason: NonEmptyText


class ReadOnlyArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="read-only",
    tag_field="kind",
):
    selector: NonEmptyLine
    reason: NonEmptyText


class UpdateRequiredArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="update-required",
    tag_field="kind",
):
    selector: NonEmptyLine
    reason: NonEmptyText


type ArchitectureImpact = NoArchitectureImpact | ReadOnlyArchitecture | UpdateRequiredArchitecture


class AcceptedScopeAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="accepted-scope",
    tag_field="kind",
):
    item_id: KebabId
    scope_revision: PositiveInt


class AuthorityAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="authority",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


class RepositoryPolicyAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="repository-policy",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


class ExistingConsumerAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="existing-consumer",
    tag_field="kind",
):
    authority_id: KebabId
    family: KebabId


type AuthorizationBasis = (
    AcceptedScopeAuthorization | AuthorityAuthorization | RepositoryPolicyAuthorization | ExistingConsumerAuthorization
)


class ContractRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    invariant: NonEmptyText
    authority: NonEmptyText
    consumer: NonEmptyText
    failure: NonEmptyText
    verification: NonEmptyText
    revalidation: NonEmptyText
    authorization_basis: AuthorizationBasis


class VerificationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authorization_basis: AuthorizationBasis
    obligation: NonEmptyText


class AcceptanceCriterion(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    number: PositiveInt
    requirement: NonEmptyText


class ReviewedAuthority(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    selector: NonEmptyLine
    reviewed_sha256: Sha256
    families: Annotated[tuple[KebabId, ...], msgspec.Meta(min_length=1)]


@dataclass(frozen=True, slots=True)
class ReviewedAuthoritySelectionFailure:
    authority_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewedAuthorityDigestMismatch:
    authority_id: str
    expected_sha256: str
    observed_sha256: str


type ReviewedAuthorityValidationFailure = ReviewedAuthoritySelectionFailure | ReviewedAuthorityDigestMismatch


class ContractCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="contract",
    tag_field="disposition",
):
    contract_invariant: NonEmptyText


class AcceptanceCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="acceptance",
    tag_field="disposition",
):
    criterion: PositiveInt


class DeferredCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="deferred",
    tag_field="disposition",
):
    deferral_id: KebabId


class NotApplicableCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="disposition",
):
    reason: NonEmptyText


type CoverageOwner = (
    ContractCoverageOwner | AcceptanceCoverageOwner | DeferredCoverageOwner | NotApplicableCoverageOwner
)


class CoverageRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    family: KebabId
    distinction: NonEmptyText
    consumer: NonEmptyText
    owner: CoverageOwner
    counterexample: NonEmptyText


class LifecycleRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: KebabId
    source_state: NonEmptyText
    authority: NonEmptyText
    evidence: NonEmptyText
    effects: NonEmptyText
    illegal_sibling: NonEmptyText


class NoLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="kind",
):
    reason: NonEmptyText


class RequiredLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="required",
    tag_field="kind",
):
    operations: Annotated[tuple[LifecycleRecord, ...], msgspec.Meta(min_length=1)]


type LifecyclePartition = NoLifecyclePartition | RequiredLifecyclePartition


class Deferral(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    deferral_id: KebabId
    reason: NonEmptyText
    reopen_when: NonEmptyText


class LocalCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="local",
    tag_field="boundary",
):
    checkpoint_id: KebabId
    title: NonEmptyLine
    architecture_impact: ArchitectureImpact
    outcome_description: NonEmptyText
    acceptance_criteria: Annotated[tuple[AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    verification: Annotated[tuple[VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[Deferral, ...]


class CrossBoundaryCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="cross-boundary",
    tag_field="boundary",
):
    checkpoint_id: KebabId
    title: NonEmptyLine
    architecture_impact: ArchitectureImpact
    outcome: Literal["independently-buildable"]
    outcome_description: NonEmptyText
    contracts: Annotated[tuple[ContractRecord, ...], msgspec.Meta(min_length=1)]
    acceptance_criteria: Annotated[tuple[AcceptanceCriterion, ...], msgspec.Meta(min_length=1)]
    reviewed_authorities: Annotated[tuple[ReviewedAuthority, ...], msgspec.Meta(min_length=1)]
    coverage: Annotated[tuple[CoverageRecord, ...], msgspec.Meta(min_length=1)]
    lifecycle_partition: LifecyclePartition
    verification: Annotated[tuple[VerificationRecord, ...], msgspec.Meta(min_length=1)]
    deferrals: tuple[Deferral, ...]


type WorkBriefCheckpoint = LocalCheckpoint | CrossBoundaryCheckpoint


def _validate_authorization(
    basis: AuthorizationBasis,
    brief: WorkBrief,
    authority_keys: frozenset[tuple[str, str]],
) -> None:
    match basis:
        case AcceptedScopeAuthorization(item_id=item_id, scope_revision=scope_revision):
            if item_id != brief.item_id or scope_revision != brief.accepted_scope.revision:
                raise ValueError("Accepted-scope authorization does not match the work brief identity.")
        case (
            AuthorityAuthorization(authority_id=authority_id, family=family)
            | RepositoryPolicyAuthorization(authority_id=authority_id, family=family)
            | ExistingConsumerAuthorization(authority_id=authority_id, family=family)
        ):
            if (authority_id, family) not in authority_keys:
                raise ValueError(f"Authorization references unknown authority family '{authority_id}#{family}'.")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_architecture_impact(impact: ArchitectureImpact) -> None:
    match impact:
        case NoArchitectureImpact():
            return
        case ReadOnlyArchitecture(selector=selector) | UpdateRequiredArchitecture(selector=selector):
            if isinstance(failure := parse_authority_selector(selector), BriefSourceFailure):
                raise ValueError(f"Architecture impact selector is invalid: {failure.message}")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_common_checkpoint(checkpoint: WorkBriefCheckpoint) -> tuple[frozenset[int], frozenset[str]]:
    _validate_architecture_impact(checkpoint.architecture_impact)
    criterion_numbers = tuple(value.number for value in checkpoint.acceptance_criteria)
    if len(set(criterion_numbers)) != len(criterion_numbers):
        raise ValueError("Acceptance criterion numbers must be unique.")
    deferral_ids = tuple(value.deferral_id for value in checkpoint.deferrals)
    if len(set(deferral_ids)) != len(deferral_ids):
        raise ValueError("Deferral identities must be unique.")
    return frozenset(criterion_numbers), frozenset(deferral_ids)


def _reviewed_authority_keys(checkpoint: CrossBoundaryCheckpoint) -> frozenset[tuple[str, str]]:
    authority_ids = tuple(value.authority_id for value in checkpoint.reviewed_authorities)
    if len(set(authority_ids)) != len(authority_ids):
        raise ValueError("Reviewed authority identities must be unique.")
    authority_keys_list = [
        (authority.authority_id, family)
        for authority in checkpoint.reviewed_authorities
        for family in authority.families
    ]
    authority_keys = frozenset(authority_keys_list)
    if len(authority_keys) != len(authority_keys_list):
        raise ValueError("Reviewed authority families must be unique.")
    for authority in checkpoint.reviewed_authorities:
        if len(set(authority.families)) != len(authority.families):
            raise ValueError(f"Reviewed authority '{authority.authority_id}' repeats a family.")
        if isinstance(failure := parse_authority_selector(authority.selector), BriefSourceFailure):
            raise ValueError(
                f"Reviewed authority '{authority.authority_id}' has an invalid selector: {failure.message}"
            )
    return authority_keys


def _validate_coverage(
    checkpoint: CrossBoundaryCheckpoint,
    authority_keys: frozenset[tuple[str, str]],
    criteria: frozenset[int],
    deferrals: frozenset[str],
    contracts: frozenset[str],
) -> None:
    coverage_keys = tuple((value.authority_id, value.family) for value in checkpoint.coverage)
    if len(set(coverage_keys)) != len(coverage_keys) or frozenset(coverage_keys) != authority_keys:
        raise ValueError("Coverage must contain exactly one record for every reviewed authority family.")
    for record in checkpoint.coverage:
        match record.owner:
            case ContractCoverageOwner(contract_invariant=invariant):
                if invariant not in contracts:
                    raise ValueError(f"Coverage names unknown contract invariant '{invariant}'.")
            case AcceptanceCoverageOwner(criterion=criterion):
                if criterion not in criteria:
                    raise ValueError(f"Coverage names unknown acceptance criterion '{criterion}'.")
            case DeferredCoverageOwner(deferral_id=deferral_id):
                if deferral_id not in deferrals:
                    raise ValueError(f"Coverage names unknown deferral '{deferral_id}'.")
                if PROHIBITION.search(record.distinction):
                    raise ValueError("An in-scope prohibition cannot be deferred.")
            case NotApplicableCoverageOwner():
                if PROHIBITION.search(record.distinction):
                    raise ValueError("An in-scope prohibition cannot be marked not applicable.")
            case _ as unreachable:
                assert_never(unreachable)


def _validate_cross_boundary_checkpoint(
    brief: WorkBrief,
    checkpoint: CrossBoundaryCheckpoint,
    criteria: frozenset[int],
    deferrals: frozenset[str],
) -> None:
    authority_keys = _reviewed_authority_keys(checkpoint)
    contract_invariants = tuple(value.invariant for value in checkpoint.contracts)
    if len(set(contract_invariants)) != len(contract_invariants):
        raise ValueError("Contract invariants must be unique.")
    for contract in checkpoint.contracts:
        _validate_authorization(contract.authorization_basis, brief, authority_keys)
    for record in checkpoint.verification:
        _validate_authorization(record.authorization_basis, brief, authority_keys)
    _validate_coverage(checkpoint, authority_keys, criteria, deferrals, frozenset(contract_invariants))
    match checkpoint.lifecycle_partition:
        case NoLifecyclePartition():
            pass
        case RequiredLifecyclePartition(operations=operations):
            operation_ids = tuple(value.operation for value in operations)
            if len(set(operation_ids)) != len(operation_ids):
                raise ValueError("Lifecycle operation identities must be unique.")
        case _ as unreachable:
            assert_never(unreachable)


def _validate_work_brief(brief: WorkBrief) -> None:
    checkpoint = brief.checkpoint
    criteria, deferrals = _validate_common_checkpoint(checkpoint)
    match checkpoint:
        case LocalCheckpoint():
            for record in checkpoint.verification:
                _validate_authorization(record.authorization_basis, brief, frozenset())
        case CrossBoundaryCheckpoint():
            _validate_cross_boundary_checkpoint(brief, checkpoint, criteria, deferrals)
        case _ as unreachable:
            assert_never(unreachable)


class WorkBrief(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief/v2"]
    artifact_revision: PositiveInt
    attempt_id: KebabId
    item_id: KebabId
    branch: NonEmptyLine
    base_revision: NonEmptyLine
    owner_task_id: NonEmptyLine
    accepted_scope: AcceptedScope
    title: NonEmptyLine
    outcome: NonEmptyText
    supported_production_roots: NonEmptyTexts
    product_decision_and_provenance: NonEmptyText
    testing_strategy: NonEmptyText
    scope: NonEmptyTexts
    bootstrap: tuple[NonEmptyText, ...]
    compatibility: tuple[NonEmptyText, ...]
    non_goals: tuple[NonEmptyText, ...]
    checkpoint: WorkBriefCheckpoint
    remaining_work: NonEmptyText

    def __post_init__(self) -> None:
        _validate_work_brief(self)


class ReviewCoverageResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: KebabId
    family: KebabId
    owner: CoverageOwner
    verdict: Literal["covered"]
    counterexample_result: NonEmptyText


class WorkBriefReview(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-review/v2"]
    attempt_id: KebabId
    checkpoint_id: KebabId
    checkpoint_sha256: Sha256
    reviewed_authority_set_sha256: Sha256
    reviewer_task_id: NonEmptyLine
    status: Literal["complete"]
    verdict: Literal["ready"]
    coverage: Annotated[tuple[ReviewCoverageResult, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        coverage_keys = tuple((record.authority_id, record.family) for record in self.coverage)
        if len(set(coverage_keys)) != len(coverage_keys):
            raise ValueError("Brief review coverage must identify every authority family at most once.")


class CheckpointIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    id: KebabId
    sha256: Sha256


class PortableArtifactIdentity(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    role: Literal["accepted-brief", "result", "implementation-review", "brief-review"]
    kind: Literal["brief", "result", "evidence"]
    key: NonEmptyLine
    revision: PositiveInt
    selector: NonEmptyLine
    content_sha256: Sha256
    size_bytes: NonNegativeInt


class LocalReviewBasis(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="local",
    tag_field="boundary",
):
    pass


class CrossBoundaryReviewBasis(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="cross-boundary",
    tag_field="boundary",
):
    brief_review: PortableArtifactIdentity
    checkpoint_sha256: Sha256
    reviewed_authority_set_sha256: Sha256


type ReviewBasis = LocalReviewBasis | CrossBoundaryReviewBasis


class CheckpointReviewPackage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-checkpoint-review-package/v1"]
    attempt_id: KebabId
    item_id: KebabId
    candidate: NonEmptyLine
    acceptance_evidence: NonEmptyLine
    accepted_scope: AcceptedScope
    checkpoint: CheckpointIdentity
    accepted_brief: PortableArtifactIdentity
    result: PortableArtifactIdentity
    implementation_review: PortableArtifactIdentity
    verdict: Literal["ready"]
    review_basis: ReviewBasis

    def __post_init__(self) -> None:
        identities = (self.accepted_brief, self.result, self.implementation_review)
        expected = (
            ("accepted-brief", "brief"),
            ("result", "result"),
            ("implementation-review", "evidence"),
        )
        if tuple((value.role, value.kind) for value in identities) != expected:
            raise ValueError("Checkpoint package artifact roles and kinds do not match their bindings.")
        match self.review_basis:
            case LocalReviewBasis():
                pass
            case CrossBoundaryReviewBasis(brief_review=brief_review, checkpoint_sha256=checkpoint_sha256):
                if (brief_review.role, brief_review.kind) != ("brief-review", "evidence"):
                    raise ValueError("Cross-boundary checkpoint packages require one brief-review Evidence identity.")
                if checkpoint_sha256 != self.checkpoint.sha256:
                    raise ValueError("Cross-boundary review basis must bind the package checkpoint digest.")
                identities = (*identities, brief_review)
            case _ as unreachable:
                assert_never(unreachable)
        portable_keys = tuple((value.kind, value.key, value.revision) for value in identities)
        if len(portable_keys) != len(set(portable_keys)):
            raise ValueError("Checkpoint package portable artifact identities must be unique.")
