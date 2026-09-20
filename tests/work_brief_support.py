import hashlib
from pathlib import Path

from msgspec.structs import replace

from pinboard.application import work_brief_models
from pinboard.application.work_briefs import (
    canonical_checkpoint_bytes,
    canonical_reviewed_authority_set_bytes,
    canonical_work_brief_bytes,
    canonical_work_brief_review_bytes,
    canonical_work_brief_review_needs_correction_bytes,
)
from pinboard.domain import work_models
from pinboard.domain.identifiers import ItemId
from tests.support import SQLITE_DIGEST, test_definition

CHECKPOINT_ID = "typed-json-cutover"


def example_work_brief() -> work_brief_models.WorkBrief:
    authority = work_brief_models.ReviewedAuthority(
        authority_id="repository-guidance",
        selector="AGENTS.md",
        reviewed_sha256="a" * 64,
        families=("repository-practice",),
    )
    contract = work_brief_models.ContractRecord(
        invariant="Strict typed JSON remains canonical.",
        authority="Repository guidance",
        consumer="Publication and dispatch",
        failure="Markdown becomes authoritative.",
        verification="Boundary and integration tests.",
        revalidation="After model or decoder changes.",
        authorization_basis=work_brief_models.AcceptedScopeAuthorization(
            item_id="make-canonical-briefs-typed-json",
            scope_revision=1,
        ),
    )
    checkpoint = work_brief_models.CrossBoundaryCheckpoint(
        checkpoint_id=CHECKPOINT_ID,
        title="Make typed JSON canonical",
        architecture_impact=work_brief_models.UpdateRequiredArchitecture(
            selector="ARCHITECTURE.md",
            reason="Canonical brief ownership changes.",
        ),
        outcome="independently-buildable",
        outcome_description="Dispatch consumes strict JSON and Markdown is a generated view.",
        disposition=work_brief_models.ContinueCheckpointDisposition(
            "Resume the separately accepted structural cleanup after this prerequisite."
        ),
        contracts=(contract,),
        acceptance_criteria=(work_brief_models.AcceptanceCriterion(1, "The typed boundary is strict."),),
        reviewed_authorities=(authority,),
        coverage=(
            work_brief_models.CoverageRecord(
                authority_id="repository-guidance",
                family="repository-practice",
                distinction="Direct msgspec decoding remains required.",
                consumer="Publication and dispatch",
                owner=work_brief_models.ContractCoverageOwner(contract_invariant=contract.invariant),
                counterexample="Decode through an untyped mapping.",
            ),
        ),
        lifecycle_partition=work_brief_models.NoLifecyclePartition(reason="No lifecycle operation changes."),
        verification=(
            work_brief_models.VerificationRecord(
                authorization_basis=work_brief_models.RepositoryPolicyAuthorization(
                    authority_id="repository-guidance",
                    family="repository-practice",
                ),
                obligation="uv run --locked pyrefly check",
            ),
        ),
        deferrals=(
            work_brief_models.Deferral("later-work", "A distinct family remains.", "This checkpoint is accepted."),
        ),
    )
    return work_brief_models.WorkBrief(
        schema="pinboard-work-brief/v4",
        artifact_revision=1,
        attempt_id="make-canonical-briefs-typed-json-1",
        item_id="make-canonical-briefs-typed-json",
        branch="codex/release-candidate",
        base_revision="2f61739541738bdd8a9ba2d484ddcdf3ab38a218",
        owner_task_id="01a04020-7d81-7602-a49e-b2d4f3ed6230",
        accepted_scope=work_brief_models.AcceptedScope(1, "b" * 64),
        title="Make canonical briefs typed JSON",
        outcome="Codex and Pinboard exchange canonical briefs as strict JSON.",
        supported_production_roots=("pinboard console script", "SQLite and immutable artifacts"),
        product_decision_and_provenance="Accepted scope revision 1 owns the v2 cutover.",
        testing_strategy="Test-first at JSON boundaries; preserve covered integration behavior.",
        scope=("Publish and dispatch strict JSON.", "Generate read-only Markdown."),
        bootstrap=("The installed v1 command launches this bridge attempt.",),
        compatibility=("Do not retain a v1 reader.",),
        non_goals=("Do not change lifecycle legality.",),
        checkpoint=checkpoint,
        checkout_selection=work_models.CheckoutSelection.MAIN,
        obligation_correspondence=(
            work_brief_models.ObligationCorrespondence(
                "next-decision",
                work_brief_models.ContractObligationTarget(contract.invariant),
            ),
        ),
    )


def work_a_brief(project: Path) -> work_brief_models.WorkBrief:
    source = project / "architecture.md"
    source.write_text("# Architecture\n\n## Contract\n\nTyped JSON is canonical.\n", encoding="utf-8")
    value = example_work_brief()
    checkpoint = value.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    authority = work_brief_models.ReviewedAuthority(
        "architecture",
        "architecture.md#Contract",
        hashlib.sha256(b"## Contract\n\nTyped JSON is canonical.\n").hexdigest(),
        ("contract",),
    )
    contract = replace(
        checkpoint.contracts[0],
        authorization_basis=work_brief_models.AcceptedScopeAuthorization("work-a", 1),
    )
    coverage = replace(
        checkpoint.coverage[0],
        authority_id="architecture",
        family="contract",
        owner=replace(checkpoint.coverage[0].owner, contract_invariant=contract.invariant),
    )
    checkpoint = replace(
        checkpoint,
        contracts=(contract,),
        reviewed_authorities=(authority,),
        coverage=(coverage,),
        verification=(
            replace(
                checkpoint.verification[0],
                authorization_basis=work_brief_models.AcceptedScopeAuthorization("work-a", 1),
            ),
        ),
    )
    return replace(
        value,
        attempt_id="work-a-1",
        item_id="work-a",
        branch="codex/work-a",
        base_revision="base-revision",
        accepted_scope=replace(value.accepted_scope, digest=SQLITE_DIGEST),
        checkpoint=checkpoint,
    )


def work_c_brief() -> work_brief_models.WorkBrief:
    candidate = example_work_brief()
    cross = candidate.checkpoint
    assert isinstance(cross, work_brief_models.CrossBoundaryCheckpoint)
    local = work_brief_models.LocalCheckpoint(
        "activate-work-c",
        "Activate work C",
        cross.architecture_impact,
        "Activate the recorded attempt with its exact typed brief.",
        cross.disposition,
        cross.acceptance_criteria,
        (
            replace(
                cross.verification[0],
                authorization_basis=work_brief_models.AcceptedScopeAuthorization("work-c", 1),
            ),
        ),
        (),
    )
    return replace(
        candidate,
        attempt_id="work-c-1",
        item_id="work-c",
        branch="codex/work-c",
        base_revision="candidate-base",
        accepted_scope=replace(candidate.accepted_scope, digest=test_definition(ItemId("work-c"))[1]),
        checkpoint=local,
        obligation_correspondence=(
            work_brief_models.ObligationCorrespondence(
                "next-decision",
                work_brief_models.CriterionObligationTarget(1),
            ),
        ),
    )


def ready_review(
    value: work_brief_models.WorkBrief,
    *,
    reviewer: str = "brief-reviewer",
    result: str = "Counterexample rejected.",
) -> bytes:
    checkpoint = value.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    coverage = checkpoint.coverage[0]
    review = work_brief_models.WorkBriefReview(
        "pinboard-work-brief-review/v3",
        value.attempt_id,
        checkpoint.checkpoint_id,
        hashlib.sha256(canonical_work_brief_bytes(value)).hexdigest(),
        hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
        hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
        reviewer,
        "complete",
        "ready",
        (
            work_brief_models.ReviewCoverageResult(
                coverage.authority_id, coverage.family, coverage.owner, "covered", result
            ),
        ),
    )
    return canonical_work_brief_review_bytes(review)


def needs_correction_review(
    value: work_brief_models.WorkBrief,
    *,
    artifact_revision: int = 1,
    reviewer: str = "brief-reviewer",
    finding_id: str = "missing-owner",
) -> bytes:
    checkpoint = value.checkpoint
    assert isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint)
    review = work_brief_models.WorkBriefReviewNeedsCorrection(
        "pinboard-work-brief-review-needs-correction/v1",
        artifact_revision,
        hashlib.sha256(canonical_work_brief_bytes(value)).hexdigest(),
        value.attempt_id,
        checkpoint.checkpoint_id,
        hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest(),
        hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest(),
        reviewer,
        "complete",
        "needs-correction",
        (
            work_brief_models.BlockingReviewFinding(
                finding_id,
                "A required owner is missing.",
                "The accepted brief omits the source that owns the effect.",
                "Add that source authority and cover its contract.",
            ),
        ),
    )
    return canonical_work_brief_review_needs_correction_bytes(review)
