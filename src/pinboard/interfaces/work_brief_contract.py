"""Machine-readable construction contract for strict work briefs."""

from typing import Literal

import msgspec

from pinboard.interfaces import work_brief_models


class WorkBriefRelationalConstraint(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    constraint_id: work_brief_models.KebabId
    rule: work_brief_models.NonEmptyText


class WorkBriefContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-contract/v1"]
    completion_rule: work_brief_models.NonEmptyText
    payload_schema: msgspec.Raw
    local_starter: msgspec.Raw
    cross_boundary_starter: msgspec.Raw
    relational_constraints: tuple[WorkBriefRelationalConstraint, ...]


class _UnresolvedAcceptedScope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: None
    digest: None


class _UnresolvedNoArchitectureImpact(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="none",
    tag_field="kind",
):
    reason: None


class _UnresolvedAcceptedScopeAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="accepted-scope",
    tag_field="kind",
):
    item_id: None
    scope_revision: None


class _UnresolvedAcceptanceCriterion(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    number: None
    requirement: None


class _UnresolvedVerificationRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authorization_basis: _UnresolvedAcceptedScopeAuthorization
    obligation: None


class _UnresolvedContractRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    invariant: None
    authority: None
    consumer: None
    failure: None
    verification: None
    revalidation: None
    authorization_basis: _UnresolvedAcceptedScopeAuthorization


class _UnresolvedReviewedAuthority(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: None
    selector: None
    reviewed_sha256: None
    families: tuple[None, ...]


class _UnresolvedContractCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="contract",
    tag_field="disposition",
):
    contract_invariant: None


class _UnresolvedCoverageRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    authority_id: None
    family: None
    distinction: None
    consumer: None
    owner: _UnresolvedContractCoverageOwner
    counterexample: None


class _UnresolvedNoLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="kind",
):
    reason: None


class _UnresolvedLocalCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="local",
    tag_field="boundary",
):
    checkpoint_id: None
    title: None
    architecture_impact: _UnresolvedNoArchitectureImpact
    outcome_description: None
    acceptance_criteria: tuple[_UnresolvedAcceptanceCriterion, ...]
    verification: tuple[_UnresolvedVerificationRecord, ...]
    deferrals: tuple[None, ...]


class _UnresolvedCrossBoundaryCheckpoint(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="cross-boundary",
    tag_field="boundary",
):
    checkpoint_id: None
    title: None
    architecture_impact: _UnresolvedNoArchitectureImpact
    outcome: Literal["independently-buildable"]
    outcome_description: None
    contracts: tuple[_UnresolvedContractRecord, ...]
    acceptance_criteria: tuple[_UnresolvedAcceptanceCriterion, ...]
    reviewed_authorities: tuple[_UnresolvedReviewedAuthority, ...]
    coverage: tuple[_UnresolvedCoverageRecord, ...]
    lifecycle_partition: _UnresolvedNoLifecyclePartition
    verification: tuple[_UnresolvedVerificationRecord, ...]
    deferrals: tuple[None, ...]


type _UnresolvedCheckpoint = _UnresolvedLocalCheckpoint | _UnresolvedCrossBoundaryCheckpoint


class _UnresolvedWorkBrief(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief/v2"]
    artifact_revision: None
    attempt_id: None
    item_id: None
    branch: None
    base_revision: None
    owner_task_id: None
    accepted_scope: _UnresolvedAcceptedScope
    title: None
    outcome: None
    supported_production_roots: tuple[None, ...]
    product_decision_and_provenance: None
    testing_strategy: None
    scope: tuple[None, ...]
    bootstrap: tuple[None, ...]
    compatibility: tuple[None, ...]
    non_goals: tuple[None, ...]
    checkpoint: _UnresolvedCheckpoint
    remaining_work: None


def _local_starter() -> _UnresolvedWorkBrief:
    return _UnresolvedWorkBrief(
        schema="pinboard-work-brief/v2",
        artifact_revision=None,
        attempt_id=None,
        item_id=None,
        branch=None,
        base_revision=None,
        owner_task_id=None,
        accepted_scope=_UnresolvedAcceptedScope(None, None),
        title=None,
        outcome=None,
        supported_production_roots=(None,),
        product_decision_and_provenance=None,
        testing_strategy=None,
        scope=(None,),
        bootstrap=(),
        compatibility=(),
        non_goals=(),
        checkpoint=_UnresolvedLocalCheckpoint(
            checkpoint_id=None,
            title=None,
            architecture_impact=_UnresolvedNoArchitectureImpact(None),
            outcome_description=None,
            acceptance_criteria=(_UnresolvedAcceptanceCriterion(None, None),),
            verification=(_UnresolvedVerificationRecord(_UnresolvedAcceptedScopeAuthorization(None, None), None),),
            deferrals=(),
        ),
        remaining_work=None,
    )


def _cross_boundary_starter() -> _UnresolvedWorkBrief:
    checkpoint = _UnresolvedCrossBoundaryCheckpoint(
        checkpoint_id=None,
        title=None,
        architecture_impact=_UnresolvedNoArchitectureImpact(None),
        outcome="independently-buildable",
        outcome_description=None,
        contracts=(
            _UnresolvedContractRecord(
                invariant=None,
                authority=None,
                consumer=None,
                failure=None,
                verification=None,
                revalidation=None,
                authorization_basis=_UnresolvedAcceptedScopeAuthorization(None, None),
            ),
        ),
        acceptance_criteria=(_UnresolvedAcceptanceCriterion(None, None),),
        reviewed_authorities=(_UnresolvedReviewedAuthority(None, None, None, (None,)),),
        coverage=(
            _UnresolvedCoverageRecord(
                authority_id=None,
                family=None,
                distinction=None,
                consumer=None,
                owner=_UnresolvedContractCoverageOwner(None),
                counterexample=None,
            ),
        ),
        lifecycle_partition=_UnresolvedNoLifecyclePartition(None),
        verification=(_UnresolvedVerificationRecord(_UnresolvedAcceptedScopeAuthorization(None, None), None),),
        deferrals=(),
    )
    local = _local_starter()
    return msgspec.structs.replace(local, checkpoint=checkpoint)


_RELATIONAL_CONSTRAINTS = (
    WorkBriefRelationalConstraint(
        "accepted-scope-identity",
        "Every accepted-scope authorization must repeat the brief item_id and accepted_scope.revision exactly.",
    ),
    WorkBriefRelationalConstraint(
        "local-verification-authorization",
        "Every local-checkpoint verification authorization must use accepted-scope and repeat the brief identity exactly.",
    ),
    WorkBriefRelationalConstraint(
        "architecture-selector",
        "A read-only or update-required architecture selector must be a valid authority selector.",
    ),
    WorkBriefRelationalConstraint(
        "unique-criteria-and-deferrals",
        "Acceptance criterion numbers and deferral identities must each be unique within the checkpoint.",
    ),
    WorkBriefRelationalConstraint(
        "unique-authority-families",
        "Reviewed authority identities must be unique; each authority family pair and every family within one authority must be unique; each selector must be valid.",
    ),
    WorkBriefRelationalConstraint(
        "unique-contracts",
        "Cross-boundary contract invariants must be unique.",
    ),
    WorkBriefRelationalConstraint(
        "authority-authorization",
        "An authority, repository-policy, or existing-consumer authorization must name a reviewed authority_id and family pair exactly.",
    ),
    WorkBriefRelationalConstraint(
        "complete-coverage",
        "Cross-boundary coverage must contain exactly one record for every reviewed authority_id and family pair and no other records.",
    ),
    WorkBriefRelationalConstraint(
        "coverage-owner",
        "Each coverage owner must name an existing contract invariant, acceptance criterion number, or deferral identity, unless it gives a not-applicable reason.",
    ),
    WorkBriefRelationalConstraint(
        "prohibition-disposition",
        "A distinction containing an in-scope prohibition cannot be deferred or marked not-applicable.",
    ),
    WorkBriefRelationalConstraint(
        "unique-lifecycle-operations",
        "When a lifecycle partition is required, operation identities must be unique.",
    ),
)


def describe_work_brief_contract() -> WorkBriefContract:
    """Return the strict schema and structural starters without choosing project facts."""
    return WorkBriefContract(
        schema="pinboard-work-brief-contract/v1",
        completion_rule=(
            "Every null is an unresolved required value. Fill it from accepted scope, reviewed authorities, and observed "
            "production consumers before publication; never infer missing facts. Empty optional collections may remain empty."
        ),
        payload_schema=msgspec.Raw(
            msgspec.json.encode(msgspec.json.schema(work_brief_models.WorkBrief), order="sorted")
        ),
        local_starter=msgspec.Raw(msgspec.json.encode(_local_starter(), order="sorted")),
        cross_boundary_starter=msgspec.Raw(msgspec.json.encode(_cross_boundary_starter(), order="sorted")),
        relational_constraints=_RELATIONAL_CONSTRAINTS,
    )
