"""Machine-readable construction contract for strict work briefs."""

from typing import Literal

import msgspec

from pinboard.interfaces import work_brief_models


class WorkBriefRelationalConstraint(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    constraint_id: work_brief_models.KebabId
    rule: work_brief_models.NonEmptyText


class WorkBriefStructuralVariant(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    selector: work_brief_models.KebabId
    template: msgspec.Raw


class WorkBriefStructuralChoice(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    choice_id: work_brief_models.KebabId
    selection_paths: tuple[work_brief_models.NonEmptyLine, ...]
    variants: tuple[WorkBriefStructuralVariant, ...]


class WorkBriefContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-contract/v1"]
    completion_rule: work_brief_models.NonEmptyText
    structural_selection_rule: work_brief_models.NonEmptyText
    canonicalization_rule: work_brief_models.NonEmptyText
    fact_validation_boundary: work_brief_models.NonEmptyText
    payload_schema: msgspec.Raw
    local_starter: msgspec.Raw
    cross_boundary_starter: msgspec.Raw
    local_structural_choices: tuple[WorkBriefStructuralChoice, ...]
    cross_boundary_structural_choices: tuple[WorkBriefStructuralChoice, ...]
    relational_constraints: tuple[WorkBriefRelationalConstraint, ...]


class WorkBriefStarterContract(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-work-brief-starter/v1"]
    boundary: Literal["local", "cross-boundary"]
    completion_rule: work_brief_models.NonEmptyText
    structural_selection_rule: work_brief_models.NonEmptyText
    canonicalization_rule: work_brief_models.NonEmptyText
    fact_validation_boundary: work_brief_models.NonEmptyText
    starter: msgspec.Raw
    structural_choices: tuple[WorkBriefStructuralChoice, ...]
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


class _UnresolvedReadOnlyArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="read-only",
    tag_field="kind",
):
    selector: None
    reason: None


class _UnresolvedUpdateRequiredArchitecture(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="update-required",
    tag_field="kind",
):
    selector: None
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


class _UnresolvedAuthorityAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="authority",
    tag_field="kind",
):
    authority_id: None
    family: None


class _UnresolvedRepositoryPolicyAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="repository-policy",
    tag_field="kind",
):
    authority_id: None
    family: None


class _UnresolvedExistingConsumerAuthorization(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="existing-consumer",
    tag_field="kind",
):
    authority_id: None
    family: None


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


class _UnresolvedAcceptanceCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="acceptance",
    tag_field="disposition",
):
    criterion: None


class _UnresolvedDeferredCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="deferred",
    tag_field="disposition",
):
    deferral_id: None


class _UnresolvedNotApplicableCoverageOwner(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="not-applicable",
    tag_field="disposition",
):
    reason: None


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


class _UnresolvedLifecycleRecord(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    operation: None
    source_state: None
    authority: None
    evidence: None
    effects: None
    illegal_sibling: None


class _UnresolvedRequiredLifecyclePartition(
    msgspec.Struct,
    frozen=True,
    forbid_unknown_fields=True,
    tag="required",
    tag_field="kind",
):
    operations: tuple[_UnresolvedLifecycleRecord, ...]


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


def _raw[T](value: T) -> msgspec.Raw:
    return msgspec.Raw(msgspec.json.encode(value, order="sorted"))


_ARCHITECTURE_CHOICE = WorkBriefStructuralChoice(
    "architecture-impact",
    ("$.checkpoint.architecture_impact",),
    (
        WorkBriefStructuralVariant("none", _raw(_UnresolvedNoArchitectureImpact(None))),
        WorkBriefStructuralVariant("read-only", _raw(_UnresolvedReadOnlyArchitecture(None, None))),
        WorkBriefStructuralVariant(
            "update-required",
            _raw(_UnresolvedUpdateRequiredArchitecture(None, None)),
        ),
    ),
)

_AUTHORIZATION_CHOICE = WorkBriefStructuralChoice(
    "authorization-basis",
    (
        "$.checkpoint.contracts[*].authorization_basis",
        "$.checkpoint.verification[*].authorization_basis",
    ),
    (
        WorkBriefStructuralVariant(
            "accepted-scope",
            _raw(_UnresolvedAcceptedScopeAuthorization(None, None)),
        ),
        WorkBriefStructuralVariant("authority", _raw(_UnresolvedAuthorityAuthorization(None, None))),
        WorkBriefStructuralVariant(
            "repository-policy",
            _raw(_UnresolvedRepositoryPolicyAuthorization(None, None)),
        ),
        WorkBriefStructuralVariant(
            "existing-consumer",
            _raw(_UnresolvedExistingConsumerAuthorization(None, None)),
        ),
    ),
)

_COVERAGE_OWNER_CHOICE = WorkBriefStructuralChoice(
    "coverage-owner",
    ("$.checkpoint.coverage[*].owner",),
    (
        WorkBriefStructuralVariant("contract", _raw(_UnresolvedContractCoverageOwner(None))),
        WorkBriefStructuralVariant("acceptance", _raw(_UnresolvedAcceptanceCoverageOwner(None))),
        WorkBriefStructuralVariant("deferred", _raw(_UnresolvedDeferredCoverageOwner(None))),
        WorkBriefStructuralVariant(
            "not-applicable",
            _raw(_UnresolvedNotApplicableCoverageOwner(None)),
        ),
    ),
)

_LIFECYCLE_PARTITION_CHOICE = WorkBriefStructuralChoice(
    "lifecycle-partition",
    ("$.checkpoint.lifecycle_partition",),
    (
        WorkBriefStructuralVariant("not-applicable", _raw(_UnresolvedNoLifecyclePartition(None))),
        WorkBriefStructuralVariant(
            "required",
            _raw(
                _UnresolvedRequiredLifecyclePartition((_UnresolvedLifecycleRecord(None, None, None, None, None, None),))
            ),
        ),
    ),
)

_LOCAL_STRUCTURAL_CHOICES = (_ARCHITECTURE_CHOICE,)
_CROSS_BOUNDARY_STRUCTURAL_CHOICES = (
    _ARCHITECTURE_CHOICE,
    _AUTHORIZATION_CHOICE,
    _COVERAGE_OWNER_CHOICE,
    _LIFECYCLE_PARTITION_CHOICE,
)


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
            "After selecting every applicable structural variant, every null is an unresolved required value. Fill it "
            "from accepted scope, reviewed authorities, and observed production consumers before publication; never "
            "infer missing facts. Empty optional collections may remain empty."
        ),
        structural_selection_rule=(
            "For every applicable selection path, choose exactly one returned variant and replace the starter value at "
            "that path with its complete template before filling nulls. A starter's existing tagged value is only the "
            "first valid option, not a fixed semantic choice. Preserve all fields outside those explicit replacements."
        ),
        canonicalization_rule=(
            "Encode the completed typed brief as JSON with lexicographically sorted object keys, no insignificant "
            "whitespace, and exactly one trailing newline."
        ),
        fact_validation_boundary=(
            "Publication validates structure, cross-references, and canonical bytes. It does not resolve branch or "
            "base_revision against Git or prove semantic scope, authority, consumer, or verification claims; the "
            "caller and independent review own those facts."
        ),
        payload_schema=msgspec.Raw(
            msgspec.json.encode(msgspec.json.schema(work_brief_models.WorkBrief), order="sorted")
        ),
        local_starter=msgspec.Raw(msgspec.json.encode(_local_starter(), order="sorted")),
        cross_boundary_starter=msgspec.Raw(msgspec.json.encode(_cross_boundary_starter(), order="sorted")),
        local_structural_choices=_LOCAL_STRUCTURAL_CHOICES,
        cross_boundary_structural_choices=_CROSS_BOUNDARY_STRUCTURAL_CHOICES,
        relational_constraints=_RELATIONAL_CONSTRAINTS,
    )


def describe_work_brief_starter(boundary: Literal["local", "cross-boundary"]) -> WorkBriefStarterContract:
    """Return one complete unresolved starter without the much larger validation schema."""
    contract = describe_work_brief_contract()
    starter = contract.local_starter if boundary == "local" else contract.cross_boundary_starter
    structural_choices = (
        contract.local_structural_choices if boundary == "local" else contract.cross_boundary_structural_choices
    )
    return WorkBriefStarterContract(
        schema="pinboard-work-brief-starter/v1",
        boundary=boundary,
        completion_rule=contract.completion_rule,
        structural_selection_rule=contract.structural_selection_rule,
        canonicalization_rule=contract.canonicalization_rule,
        fact_validation_boundary=contract.fact_validation_boundary,
        starter=starter,
        structural_choices=structural_choices,
        relational_constraints=contract.relational_constraints,
    )
