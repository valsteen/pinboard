import hashlib
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.application import query_models, stored_state
from pinboard.application.artifact_publication import ArtifactReader
from pinboard.application.artifacts import BriefArtifactRef, WorkBriefIdentity
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import AttemptId
from pinboard.interfaces import work_brief_models
from pinboard.interfaces.brief_source_models import authority_selector
from pinboard.interfaces.brief_sources import select_brief_source
from pinboard.interfaces.errors import (
    BriefSourceFailure,
    WorkBriefErrorCode,
    WorkBriefFailure,
    WorkBriefResult,
)


def _invalid(message: str) -> WorkBriefFailure:
    return WorkBriefFailure(WorkBriefErrorCode.BRIEF_INVALID, message)


def _canonical_bytes[T](value: T) -> bytes:
    return msgspec.json.encode(value, order="sorted")


def _owner_key(owner: work_brief_models.CoverageOwner) -> tuple[str, str | int]:
    match owner:
        case work_brief_models.ContractCoverageOwner(contract_invariant=invariant):
            return "contract", invariant
        case work_brief_models.AcceptanceCoverageOwner(criterion=criterion):
            return "acceptance", criterion
        case work_brief_models.DeferredCoverageOwner(deferral_id=deferral_id):
            return "deferred", deferral_id
        case work_brief_models.NotApplicableCoverageOwner(reason=reason):
            return "not-applicable", reason
        case _ as unreachable:
            assert_never(unreachable)


def decode_work_brief(data: bytes) -> WorkBriefResult[work_brief_models.WorkBrief]:
    try:
        return msgspec.json.decode(data, type=work_brief_models.WorkBrief)
    except msgspec.DecodeError as error:
        return _invalid(f"Cannot decode canonical work brief: {error}")


def canonical_work_brief_bytes(brief: work_brief_models.WorkBrief) -> bytes:
    return _canonical_bytes(brief) + b"\n"


def decode_canonical_work_brief(data: bytes) -> WorkBriefResult[work_brief_models.WorkBrief]:
    brief = decode_work_brief(data)
    if isinstance(brief, WorkBriefFailure):
        return brief
    if data != canonical_work_brief_bytes(brief):
        return WorkBriefFailure(
            WorkBriefErrorCode.BRIEF_NOT_CANONICAL,
            "Accepted work brief bytes are not the canonical msgspec encoding.",
        )
    return brief


def canonical_checkpoint_bytes(checkpoint: work_brief_models.WorkBriefCheckpoint) -> bytes:
    return _canonical_bytes(checkpoint)


def canonical_reviewed_authority_set_bytes(authorities: tuple[work_brief_models.ReviewedAuthority, ...]) -> bytes:
    return _canonical_bytes(authorities)


def validate_reviewed_authority_digests(
    source_checkout_root: Path,
    authorities: tuple[work_brief_models.ReviewedAuthority, ...],
) -> work_brief_models.ReviewedAuthorityValidationFailure | None:
    for authority in authorities:
        selected = select_brief_source(
            source_checkout_root,
            authority_selector(authority.selector),
            require_utf8=True,
        )
        if isinstance(selected, BriefSourceFailure):
            return work_brief_models.ReviewedAuthoritySelectionFailure(authority.authority_id, selected.message)
        observed_sha256 = hashlib.sha256(selected.content).hexdigest()
        if observed_sha256 != authority.reviewed_sha256:
            return work_brief_models.ReviewedAuthorityDigestMismatch(
                authority.authority_id,
                authority.reviewed_sha256,
                observed_sha256,
            )
    return None


def decode_work_brief_review(data: bytes) -> WorkBriefResult[work_brief_models.WorkBriefReview]:
    try:
        return msgspec.json.decode(data, type=work_brief_models.WorkBriefReview)
    except msgspec.DecodeError as error:
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode canonical work brief review: {error}",
        )


def canonical_work_brief_review_bytes(review: work_brief_models.WorkBriefReview) -> bytes:
    return _canonical_bytes(review) + b"\n"


def decode_canonical_work_brief_review(data: bytes) -> WorkBriefResult[work_brief_models.WorkBriefReview]:
    review = decode_work_brief_review(data)
    if isinstance(review, WorkBriefFailure):
        return review
    if data != canonical_work_brief_review_bytes(review):
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Accepted work brief review bytes are not the canonical msgspec encoding.",
        )
    return review


def decode_checkpoint_review_package(data: bytes) -> WorkBriefResult[work_brief_models.CheckpointReviewPackage]:
    try:
        return msgspec.json.decode(data, type=work_brief_models.CheckpointReviewPackage)
    except msgspec.DecodeError as error:
        return WorkBriefFailure(
            WorkBriefErrorCode.PACKAGE_INVALID,
            f"Cannot decode checkpoint review package: {error}",
        )


def canonical_checkpoint_review_package_bytes(package: work_brief_models.CheckpointReviewPackage) -> bytes:
    return _canonical_bytes(package) + b"\n"


def decode_canonical_checkpoint_review_package(
    data: bytes,
) -> WorkBriefResult[work_brief_models.CheckpointReviewPackage]:
    package = decode_checkpoint_review_package(data)
    if isinstance(package, WorkBriefFailure):
        return package
    if data != canonical_checkpoint_review_package_bytes(package):
        return WorkBriefFailure(
            WorkBriefErrorCode.PACKAGE_NOT_CANONICAL,
            "Checkpoint review package bytes are not the canonical msgspec encoding.",
        )
    return package


def decode_completion_review_package(data: bytes) -> WorkBriefResult[work_brief_models.CompletionReviewPackage]:
    try:
        return msgspec.json.decode(data, type=work_brief_models.CompletionReviewPackage)
    except msgspec.DecodeError as error:
        return WorkBriefFailure(
            WorkBriefErrorCode.PACKAGE_INVALID,
            f"Cannot decode completion review package: {error}",
        )


def canonical_completion_review_package_bytes(package: work_brief_models.CompletionReviewPackage) -> bytes:
    return _canonical_bytes(package) + b"\n"


def decode_canonical_completion_review_package(
    data: bytes,
) -> WorkBriefResult[work_brief_models.CompletionReviewPackage]:
    package = decode_completion_review_package(data)
    if isinstance(package, WorkBriefFailure):
        return package
    if data != canonical_completion_review_package_bytes(package):
        return WorkBriefFailure(
            WorkBriefErrorCode.PACKAGE_NOT_CANONICAL,
            "Completion review package bytes are not the canonical msgspec encoding.",
        )
    return package


def validate_work_brief_review(
    review: work_brief_models.WorkBriefReview,
    brief: work_brief_models.WorkBrief,
    reviewer_task_id: str | None = None,
) -> WorkBriefFailure | None:
    checkpoint = brief.checkpoint
    if not isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        return WorkBriefFailure(WorkBriefErrorCode.REVIEW_INVALID, "Local checkpoints do not use brief reviews.")
    if review.attempt_id != brief.attempt_id or review.checkpoint_id != checkpoint.checkpoint_id:
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_INVALID,
            "Brief review names a different attempt or checkpoint.",
        )
    owner_task_id = brief.owner_task_id if reviewer_task_id is None else reviewer_task_id
    if review.reviewer_task_id == owner_task_id:
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT,
            "The brief reviewer must be a different task from the attempt owner.",
        )
    if review.checkpoint_sha256 != hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest() or (
        review.reviewed_authority_set_sha256
        != hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest()
    ):
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_STALE,
            "Brief review is not bound to the current checkpoint and reviewed authorities.",
        )
    expected = {(record.authority_id, record.family, _owner_key(record.owner)) for record in checkpoint.coverage}
    observed = {(record.authority_id, record.family, _owner_key(record.owner)) for record in review.coverage}
    if len(observed) != len(review.coverage) or observed != expected:
        return WorkBriefFailure(
            WorkBriefErrorCode.REVIEW_NOT_READY,
            "Brief review must contain exactly one covered result for every coverage owner.",
        )
    return None


def _authorization_text(
    basis: work_brief_models.AcceptedScopeAuthorization
    | work_brief_models.AuthorityAuthorization
    | work_brief_models.RepositoryPolicyAuthorization
    | work_brief_models.ExistingConsumerAuthorization,
) -> str:
    match basis:
        case work_brief_models.AcceptedScopeAuthorization(item_id=item_id, scope_revision=revision):
            return f"accepted-scope:{item_id}@{revision}"
        case work_brief_models.AuthorityAuthorization(authority_id=authority_id, family=family):
            return f"authority:{authority_id}#{family}"
        case work_brief_models.RepositoryPolicyAuthorization(authority_id=authority_id, family=family):
            return f"repository-policy:{authority_id}#{family}"
        case work_brief_models.ExistingConsumerAuthorization(authority_id=authority_id, family=family):
            return f"existing-consumer:{authority_id}#{family}"
        case _ as unreachable:
            assert_never(unreachable)


def _architecture_text(impact: work_brief_models.ArchitectureImpact) -> str:
    match impact:
        case work_brief_models.NoArchitectureImpact(reason=reason):
            return f"none — {reason}"
        case work_brief_models.ReadOnlyArchitecture(selector=selector, reason=reason):
            return f"read-only — `{selector}` — {reason}"
        case work_brief_models.UpdateRequiredArchitecture(selector=selector, reason=reason):
            return f"update-required — `{selector}` — {reason}"
        case _ as unreachable:
            assert_never(unreachable)


def _section(lines: list[str], heading: str, values: tuple[str, ...]) -> None:
    lines.extend((f"## {heading}", ""))
    lines.extend(f"- {value}" for value in values)
    lines.append("")


def render_work_brief_markdown(brief: work_brief_models.WorkBrief) -> bytes:
    checkpoint = brief.checkpoint
    lines = [
        "---",
        "kind: work-attempt-view",
        "authority: pinboard-work-brief/v2",
        f"attempt: {brief.attempt_id}",
        f"item_id: {brief.item_id}",
        f"branch: {brief.branch}",
        f"base_revision: {brief.base_revision}",
        f"owner_task_id: {brief.owner_task_id}",
        f"accepted_scope_revision: {brief.accepted_scope.revision}",
        f"accepted_scope_digest: {brief.accepted_scope.digest}",
        f"artifact_revision: {brief.artifact_revision}",
        "---",
        "",
        "> Generated projection; canonical JSON is authoritative.",
        "",
        f"# {brief.title}",
        "",
        brief.outcome,
        "",
        f"## Checkpoint: {checkpoint.title}",
        "",
        f"- Checkpoint ID: `{checkpoint.checkpoint_id}`",
        f"- Boundary: `{('cross-boundary' if isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint) else 'local')}`",
        f"- Architecture impact: {_architecture_text(checkpoint.architecture_impact)}",
        "",
        checkpoint.outcome_description,
        "",
    ]
    _section(lines, "Supported production roots", brief.supported_production_roots)
    _section(lines, "Scope", brief.scope)
    _section(lines, "Bootstrap", brief.bootstrap)
    _section(lines, "Compatibility", brief.compatibility)
    _section(lines, "Non-goals", brief.non_goals)
    lines.extend(("## Product decision and provenance", "", brief.product_decision_and_provenance, ""))
    lines.extend(("## Testing strategy", "", brief.testing_strategy, ""))
    if isinstance(checkpoint, work_brief_models.CrossBoundaryCheckpoint):
        lines.extend(("## Contract", ""))
        for record in checkpoint.contracts:
            lines.extend(
                (
                    f"### {record.invariant}",
                    "",
                    f"- Authority: {record.authority}",
                    f"- Consumer: {record.consumer}",
                    f"- Failure: {record.failure}",
                    f"- Verification: {record.verification}",
                    f"- Revalidation: {record.revalidation}",
                    f"- Authorization: `{_authorization_text(record.authorization_basis)}`",
                    "",
                )
            )
        lines.extend(("## Reviewed authorities", ""))
        lines.extend(
            (
                f"- `{authority.authority_id}` — `{authority.selector}` — `{authority.reviewed_sha256}` — "
                + ", ".join(f"`{family}`" for family in authority.families)
            )
            for authority in checkpoint.reviewed_authorities
        )
        lines.extend(("", "## Authoritative coverage", ""))
        for record in checkpoint.coverage:
            lines.extend(
                (
                    f"### {record.authority_id}#{record.family}",
                    "",
                    f"- Distinction: {record.distinction}",
                    f"- Consumer: {record.consumer}",
                    f"- Owner: `{_owner_key(record.owner)[0]}:{_owner_key(record.owner)[1]}`",
                    f"- Counterexample: {record.counterexample}",
                    "",
                )
            )
        match checkpoint.lifecycle_partition:
            case work_brief_models.NoLifecyclePartition(reason=reason):
                lines.extend(("## Lifecycle partition", "", f"Not applicable — {reason}", ""))
            case work_brief_models.RequiredLifecyclePartition(operations=operations):
                lines.extend(("## Lifecycle partition", ""))
                for operation in operations:
                    lines.extend(
                        (
                            f"### {operation.operation}",
                            "",
                            f"- Source state: {operation.source_state}",
                            f"- Authority: {operation.authority}",
                            f"- Evidence: {operation.evidence}",
                            f"- Effects: {operation.effects}",
                            f"- Illegal sibling: {operation.illegal_sibling}",
                            "",
                        )
                    )
            case _ as unreachable:
                assert_never(unreachable)
    lines.extend(("## Acceptance criteria", ""))
    lines.extend(f"{value.number}. {value.requirement}" for value in checkpoint.acceptance_criteria)
    lines.extend(("", "## Verification", ""))
    lines.extend(
        f"- `{_authorization_text(value.authorization_basis)}` — `{value.obligation}`"
        for value in checkpoint.verification
    )
    lines.extend(("", "## Deferrals", ""))
    lines.extend(
        f"- `{value.deferral_id}` — {value.reason} Reopen when: {value.reopen_when}" for value in checkpoint.deferrals
    )
    lines.extend(("", "## Remaining work", "", brief.remaining_work, ""))
    return "\n".join(lines).encode()


def decode_work_brief_identity(data: bytes) -> WorkBriefResult[WorkBriefIdentity]:
    brief = decode_canonical_work_brief(data)
    if isinstance(brief, WorkBriefFailure):
        return brief
    return WorkBriefIdentity(
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    )


def read_selected_work_brief_identity(
    reference: stored_state.ArtifactReference | BriefArtifactRef | None,
    artifacts: ArtifactReader,
) -> DecisionResult[WorkBriefIdentity | None]:
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return None
    identity = decode_work_brief_identity(artifacts.read(reference))
    if isinstance(identity, WorkBriefFailure):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"The selected brief artifact is not a valid canonical typed work brief: {identity}",
            None,
        )
    return identity


def _render_attempt_brief_view(
    attempt: stored_state.StoredAttempt,
    reference: stored_state.ArtifactReference | BriefArtifactRef | None,
    artifacts: ArtifactReader,
) -> WorkBriefResult[bytes]:
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return _invalid(f"Live attempt '{attempt.attempt_id}' has no accepted brief reference.")
    if not reference.selector.endswith(".json"):
        return _invalid(f"Live attempt '{attempt.attempt_id}' accepted brief is not canonical v2 JSON.")
    brief = decode_canonical_work_brief(artifacts.read(reference))
    if isinstance(brief, WorkBriefFailure):
        return brief
    expected = (
        str(attempt.attempt_id),
        str(attempt.item_id),
        attempt.branch,
        attempt.base_revision,
        attempt.accepted_scope_revision,
        attempt.accepted_scope_digest,
    )
    observed = (
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    )
    if observed != expected:
        return _invalid(f"Live attempt '{attempt.attempt_id}' brief identity does not match SQLite.")
    return render_work_brief_markdown(brief)


def build_attempt_brief_views(
    state: stored_state.StoredWorkState, artifacts: ArtifactReader
) -> WorkBriefResult[dict[AttemptId, bytes]]:
    result: dict[AttemptId, bytes] = {}
    references = {value.artifact_ref_id: value for value in state.artifact_references}
    for attempt in state.lifecycle.attempts:
        if attempt.state == work_models.AttemptState.DONE:
            continue
        reference = references.get(attempt.brief_artifact_ref_id)
        rendered = _render_attempt_brief_view(attempt, reference, artifacts)
        if isinstance(rendered, WorkBriefFailure):
            return rendered
        result[attempt.attempt_id] = rendered
    return result


def build_selected_attempt_brief_views(
    attempts: tuple[query_models.AttemptProjectionFacts, ...], artifacts: ArtifactReader
) -> WorkBriefResult[dict[AttemptId, bytes]]:
    """Render only the accepted briefs required by selected attempt views."""

    result: dict[AttemptId, bytes] = {}
    for selected in attempts:
        attempt = selected.attempt
        if attempt.state == work_models.AttemptState.DONE:
            continue
        rendered = _render_attempt_brief_view(attempt, selected.brief_reference, artifacts)
        if isinstance(rendered, WorkBriefFailure):
            return rendered
        result[attempt.attempt_id] = rendered
    return result
