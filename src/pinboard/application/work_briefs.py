import hashlib
from datetime import datetime
from typing import assert_never

import msgspec

from pinboard.application import (
    checkpoint_compatibility_models,
    query_models,
    stored_state,
    work_brief_compatibility_models,
    work_brief_models,
)
from pinboard.application.artifact_publication import (
    AcceptedArtifactPublication,
    ArtifactPublisher,
    ArtifactReader,
    publish_accepted_artifact,
)
from pinboard.application.artifacts import (
    BriefArtifactRef,
    CurrentAttemptWorkBriefIdentity,
    NewArtifact,
    WorkBriefIdentity,
)
from pinboard.application.brief_source_models import BriefSourceFailure, authority_selector
from pinboard.application.brief_sources import BriefSourceSelector
from pinboard.application.ports import WorkStore
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, ItemId

type WorkBriefValue = (
    work_brief_models.WorkBrief
    | work_brief_compatibility_models.WorkBriefV3
    | work_brief_compatibility_models.WorkBriefV2
)
type WorkBriefReviewValue = work_brief_models.WorkBriefReview | work_brief_compatibility_models.WorkBriefReviewV2

type CheckpointPackage = (
    checkpoint_compatibility_models.CheckpointReviewPackage
    | checkpoint_compatibility_models.CheckpointReviewPackageV2
    | work_brief_models.CheckpointReviewPackageV3
)


def publish_work_brief(
    store: WorkStore,
    publisher: ArtifactPublisher,
    brief: work_brief_models.WorkBrief,
    accepted_at: datetime,
) -> AcceptedArtifactPublication | DecisionFailure | work_brief_models.WorkBriefFailure:
    if (failure := _validate_current_definition(store, brief)) is not None:
        return failure
    return publish_accepted_artifact(
        store,
        publisher,
        NewArtifact(
            work_models.ArtifactKind.BRIEF,
            brief.attempt_id,
            brief.artifact_revision,
            ".json",
            canonical_work_brief_bytes(brief),
        ),
        accepted_at,
    )


def _invalid(message: str) -> work_brief_models.WorkBriefFailure:
    return work_brief_models.WorkBriefFailure(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, message)


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


def decode_work_brief(data: bytes) -> work_brief_models.WorkBriefResult[WorkBriefValue]:
    try:
        schema_raw = msgspec.json.decode(data, type=dict[str, msgspec.Raw]).get("schema")
        schema = None if schema_raw is None else msgspec.json.decode(schema_raw, type=str)
        if schema == "pinboard-work-brief/v2":
            return msgspec.json.decode(data, type=work_brief_compatibility_models.WorkBriefV2)
        if schema == "pinboard-work-brief/v3":
            return msgspec.json.decode(data, type=work_brief_compatibility_models.WorkBriefV3)
        return msgspec.json.decode(data, type=work_brief_models.WorkBrief)
    except msgspec.DecodeError as error:
        return _invalid(f"Cannot decode canonical work brief: {error}")


def canonical_work_brief_bytes(brief: work_brief_models.ReadableWorkBrief) -> bytes:
    return _canonical_bytes(brief) + b"\n"


def decode_canonical_work_brief(data: bytes) -> work_brief_models.WorkBriefResult[WorkBriefValue]:
    brief = decode_work_brief(data)
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return brief
    if data != canonical_work_brief_bytes(brief):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.BRIEF_NOT_CANONICAL,
            "Accepted work brief bytes are not the canonical msgspec encoding.",
        )
    return brief


def validate_definition_brief_agreement(
    definition: work_models.WorkItemDefinition,
    brief: work_brief_models.WorkBrief,
) -> work_brief_models.WorkBriefFailure | None:
    if definition.checkout_policy == work_models.CheckoutPolicy.LEGACY_UNRECORDED:
        return _invalid("Current work briefs require a current definition with explicit checkout policy.")
    expected_ids = tuple(value.obligation_id for value in definition.obligations)
    observed_ids = tuple(work_models.ObligationId(value.obligation_id) for value in brief.obligation_correspondence)
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != set(expected_ids):
        return _invalid("Obligation correspondence must contain exactly one row for every accepted obligation.")
    policies = {value.obligation_id: value.deferral_policy for value in definition.obligations}
    if any(
        isinstance(row.target, work_brief_models.DeferralObligationTarget)
        and policies[work_models.ObligationId(row.obligation_id)] == work_models.ObligationDeferralPolicy.FORBIDDEN
        for row in brief.obligation_correspondence
    ):
        return _invalid("An obligation whose deferral is forbidden cannot map to a deferral.")
    expected_selection = {
        work_models.CheckoutPolicy.MAIN: work_models.CheckoutSelection.MAIN,
        work_models.CheckoutPolicy.ISOLATED: work_models.CheckoutSelection.ISOLATED,
    }.get(definition.checkout_policy)
    if expected_selection is not None and brief.checkout_selection != expected_selection:
        return _invalid("The resolved checkout selection does not match the fixed definition policy.")
    return None


def _validate_current_definition(
    store: WorkStore,
    brief: work_brief_models.WorkBrief,
) -> work_brief_models.WorkBriefFailure | None:
    selected = store.read_item_definition(ItemId(brief.item_id))
    definition = selected.definition
    if (
        definition is None
        or definition.revision != brief.accepted_scope.revision
        or definition.digest != brief.accepted_scope.digest
    ):
        return _invalid("The work brief does not name the exact current accepted definition.")
    return validate_definition_brief_agreement(definition.definition, brief)


def validate_executable_work_brief(
    store: WorkStore,
    brief: WorkBriefValue,
    observed_checkout: work_models.CheckoutSelection,
) -> work_brief_models.WorkBriefFailure | None:
    """Require the exact current definition, current brief schema, and selected checkout."""

    if not isinstance(brief, work_brief_models.WorkBrief):
        return _invalid("Retained work brief v3/v2 is readable but cannot authorize execution.")
    if (failure := _validate_current_definition(store, brief)) is not None:
        return failure
    if brief.checkout_selection != observed_checkout:
        return _invalid("The selected source checkout does not match the checkout recorded by the work brief.")
    return None


def canonical_checkpoint_bytes(checkpoint: work_brief_models.ReadableCheckpoint) -> bytes:
    return _canonical_bytes(checkpoint)


def canonical_reviewed_authority_set_bytes(authorities: tuple[work_brief_models.ReviewedAuthority, ...]) -> bytes:
    return _canonical_bytes(authorities)


def validate_reviewed_authority_digests(
    select_source: BriefSourceSelector,
    authorities: tuple[work_brief_models.ReviewedAuthority, ...],
) -> work_brief_models.ReviewedAuthorityValidationFailure | None:
    for authority in authorities:
        selected = select_source(authority_selector(authority.selector), True)
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


def decode_work_brief_review(data: bytes) -> work_brief_models.WorkBriefResult[WorkBriefReviewValue]:
    try:
        schema_raw = msgspec.json.decode(data, type=dict[str, msgspec.Raw]).get("schema")
        schema = None if schema_raw is None else msgspec.json.decode(schema_raw, type=str)
        if schema == "pinboard-work-brief-review/v2":
            return msgspec.json.decode(data, type=work_brief_compatibility_models.WorkBriefReviewV2)
        return msgspec.json.decode(data, type=work_brief_models.WorkBriefReview)
    except (msgspec.DecodeError, ValueError) as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode canonical work brief review: {error}",
        )


def canonical_work_brief_review_bytes(review: WorkBriefReviewValue) -> bytes:
    return _canonical_bytes(review) + b"\n"


def canonical_correction_source_review_bytes(review: work_brief_models.CorrectionSourceReview) -> bytes:
    return _canonical_bytes(review) + b"\n"


def canonical_candidate_review_bytes(review: work_brief_models.CandidateReview) -> bytes:
    return _canonical_bytes(review) + b"\n"


def decode_canonical_candidate_review(
    data: bytes,
) -> work_brief_models.WorkBriefResult[work_brief_models.CandidateReview]:
    try:
        review = msgspec.json.decode(data, type=work_brief_models.CandidateReview)
    except (msgspec.DecodeError, ValueError) as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode canonical candidate review: {error}",
        )
    if data != canonical_candidate_review_bytes(review):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Accepted candidate review bytes are not the canonical msgspec encoding.",
        )
    return review


def candidate_review_key(
    attempt_id: str,
    candidate: str,
    candidate_snapshot_sha256: str,
    accepted_brief_sha256: str,
    result_sha256: str,
    review_sha256: str,
) -> str:
    identity = _canonical_bytes(
        (
            attempt_id,
            candidate,
            candidate_snapshot_sha256,
            accepted_brief_sha256,
            result_sha256,
            review_sha256,
        )
    )
    return f"candidate-review-{hashlib.sha256(identity).hexdigest()}"


def validate_candidate_review(
    review: work_brief_models.CandidateReview,
    *,
    brief: work_brief_models.ReadableWorkBrief,
    candidate: str,
    candidate_snapshot: stored_state.ArtifactReference,
    accepted_brief: stored_state.ArtifactReference | BriefArtifactRef,
    result_sha256: str,
    review_sha256: str,
) -> work_brief_models.WorkBriefFailure | None:
    snapshot = review.candidate_snapshot
    selected_brief = review.accepted_brief
    if (
        review.attempt_id,
        review.item_id,
        review.candidate,
        review.result_sha256,
        review.review_sha256,
    ) != (
        brief.attempt_id,
        brief.item_id,
        candidate,
        result_sha256,
        review_sha256,
    ):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
            "Candidate review is not bound to the current attempt, candidate, result, and review bytes.",
        )
    if review.reviewer_task_id == brief.owner_task_id:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT,
            "The candidate reviewer must be a different task from the attempt owner.",
        )
    if (
        snapshot.key,
        snapshot.revision,
        snapshot.selector,
        snapshot.content_sha256,
        snapshot.size_bytes,
        selected_brief.key,
        selected_brief.revision,
        selected_brief.selector,
        selected_brief.content_sha256,
        selected_brief.size_bytes,
    ) != (
        candidate_snapshot.key,
        candidate_snapshot.revision,
        candidate_snapshot.selector,
        candidate_snapshot.content_sha256,
        candidate_snapshot.size_bytes,
        accepted_brief.key,
        accepted_brief.revision,
        accepted_brief.selector,
        accepted_brief.content_sha256,
        accepted_brief.size_bytes,
    ):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
            "Candidate review is not bound to the current accepted candidate snapshot and brief.",
        )
    return None


def decode_canonical_work_brief_review(
    data: bytes,
) -> work_brief_models.WorkBriefResult[WorkBriefReviewValue]:
    review = decode_work_brief_review(data)
    if isinstance(review, work_brief_models.WorkBriefFailure):
        return review
    if data != canonical_work_brief_review_bytes(review):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Accepted work brief review bytes are not the canonical msgspec encoding.",
        )
    return review


def decode_work_brief_review_needs_correction(
    data: bytes,
) -> work_brief_models.WorkBriefResult[work_brief_models.WorkBriefReviewNeedsCorrection]:
    try:
        return msgspec.json.decode(data, type=work_brief_models.WorkBriefReviewNeedsCorrection)
    except msgspec.DecodeError as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            f"Cannot decode canonical needs-correction work brief review: {error}",
        )


def canonical_work_brief_review_needs_correction_bytes(
    review: work_brief_models.WorkBriefReviewNeedsCorrection,
) -> bytes:
    return _canonical_bytes(review) + b"\n"


def decode_canonical_work_brief_review_needs_correction(
    data: bytes,
) -> work_brief_models.WorkBriefResult[work_brief_models.WorkBriefReviewNeedsCorrection]:
    review = decode_work_brief_review_needs_correction(data)
    if isinstance(review, work_brief_models.WorkBriefFailure):
        return review
    if data != canonical_work_brief_review_needs_correction_bytes(review):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_CANONICAL,
            "Accepted needs-correction work brief review bytes are not the canonical msgspec encoding.",
        )
    return review


def needs_correction_review_key(brief: work_brief_models.ReadableWorkBrief) -> str:
    checkpoint = brief.checkpoint
    if not isinstance(
        checkpoint,
        (work_brief_models.CrossBoundaryCheckpoint, work_brief_compatibility_models.CrossBoundaryCheckpointV3),
    ):
        raise ValueError("Local checkpoints do not use needs-correction brief reviews.")
    brief_sha256 = hashlib.sha256(canonical_work_brief_bytes(brief)).hexdigest()
    checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
    identity_sha256 = hashlib.sha256(
        _canonical_bytes((brief.attempt_id, checkpoint.checkpoint_id, brief_sha256, checkpoint_sha256))
    ).hexdigest()
    return f"brief-review-needs-correction-{identity_sha256}"


def read_accepted_work_brief(
    store: WorkStore,
    reader: ArtifactReader,
    brief_artifact_ref_id: ArtifactRefId,
) -> work_brief_models.WorkBriefResult[work_brief_models.AcceptedWorkBrief]:
    """Resolve the exact accepted reference and decode its verified canonical bytes."""
    reference = store.read_artifact_reference_by_id(brief_artifact_ref_id)
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.BRIEF_INVALID,
            "The selected accepted brief reference does not exist or is not a brief.",
        )
    brief = decode_canonical_work_brief(reader.read(reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return brief
    return work_brief_models.AcceptedWorkBrief(reference, brief)


def publish_brief_review_needs_correction(
    store: WorkStore,
    reader: ArtifactReader,
    publisher: ArtifactPublisher,
    brief_artifact_ref_id: ArtifactRefId,
    review: work_brief_models.WorkBriefReviewNeedsCorrection,
    accepted_at: datetime,
) -> AcceptedArtifactPublication | DecisionFailure | work_brief_models.WorkBriefFailure:
    """Validate an independent negative review, publish bytes, then accept their reference.

    Artifact acceptance retains its irreversible-publication failure contract. This operation
    does not acquire authority, change lifecycle, or establish dispatch readiness.
    """
    selected = read_accepted_work_brief(store, reader, brief_artifact_ref_id)
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return selected
    if (failure := validate_work_brief_review_needs_correction(review, selected.brief)) is not None:
        return failure
    return publish_accepted_artifact(
        store,
        publisher,
        NewArtifact(
            work_models.ArtifactKind.EVIDENCE,
            needs_correction_review_key(selected.brief),
            review.artifact_revision,
            ".json",
            canonical_work_brief_review_needs_correction_bytes(review),
        ),
        accepted_at,
    )


def read_brief_review_status(
    store: WorkStore,
    reader: ArtifactReader,
    brief_artifact_ref_id: ArtifactRefId,
) -> work_brief_models.WorkBriefResult[work_brief_models.BriefReviewStatus]:
    """Read exact accepted brief and latest verified negative evidence without mutation."""
    selected = read_accepted_work_brief(store, reader, brief_artifact_ref_id)
    if isinstance(selected, work_brief_models.WorkBriefFailure):
        return selected
    checkpoint = _review_checkpoint(selected.brief)
    if isinstance(checkpoint, work_brief_models.WorkBriefFailure):
        return checkpoint
    reference = store.read_latest_artifact_reference(
        work_models.ArtifactKind.EVIDENCE, needs_correction_review_key(selected.brief)
    )
    if reference is None:
        return work_brief_models.NoNeedsCorrectionEvidence(selected)
    review = decode_canonical_work_brief_review_needs_correction(reader.read(reference))
    if isinstance(review, work_brief_models.WorkBriefFailure):
        return review
    if (failure := validate_work_brief_review_needs_correction(review, selected.brief)) is not None:
        return failure
    return work_brief_models.NeedsCorrectionEvidence(selected, reference, review)


def _review_checkpoint(
    brief: work_brief_models.ReadableWorkBrief,
) -> (
    work_brief_models.CrossBoundaryCheckpoint
    | work_brief_compatibility_models.CrossBoundaryCheckpointV3
    | work_brief_models.WorkBriefFailure
):
    checkpoint = brief.checkpoint
    if isinstance(
        checkpoint,
        (work_brief_models.CrossBoundaryCheckpoint, work_brief_compatibility_models.CrossBoundaryCheckpointV3),
    ):
        return checkpoint
    return work_brief_models.WorkBriefFailure(
        work_brief_models.WorkBriefErrorCode.REVIEW_INVALID, "Local checkpoints do not use brief reviews."
    )


def validate_work_brief_review_needs_correction(
    review: work_brief_models.WorkBriefReviewNeedsCorrection,
    brief: work_brief_models.ReadableWorkBrief,
) -> work_brief_models.WorkBriefFailure | None:
    checkpoint = _review_checkpoint(brief)
    if isinstance(checkpoint, work_brief_models.WorkBriefFailure):
        return checkpoint
    if review.attempt_id != brief.attempt_id or review.checkpoint_id != checkpoint.checkpoint_id:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            "Needs-correction brief review names a different attempt or checkpoint.",
        )
    if review.reviewer_task_id == brief.owner_task_id:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT,
            "The brief reviewer must be a different task from the attempt owner.",
        )
    expected_brief_sha256 = hashlib.sha256(canonical_work_brief_bytes(brief)).hexdigest()
    expected_checkpoint_sha256 = hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
    expected_authority_sha256 = hashlib.sha256(
        canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)
    ).hexdigest()
    if (
        review.accepted_brief_sha256 != expected_brief_sha256
        or review.checkpoint_sha256 != expected_checkpoint_sha256
        or review.reviewed_authority_set_sha256 != expected_authority_sha256
    ):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
            "Needs-correction brief review is not bound to the exact accepted brief, checkpoint, and reviewed authorities.",
        )
    return None


def decode_checkpoint_review_package(data: bytes) -> work_brief_models.WorkBriefResult[CheckpointPackage]:
    try:
        return msgspec.json.decode(
            data,
            type=checkpoint_compatibility_models.CheckpointReviewPackage
            | checkpoint_compatibility_models.CheckpointReviewPackageV2
            | work_brief_models.CheckpointReviewPackageV3,
        )
    except msgspec.DecodeError as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID,
            f"Cannot decode checkpoint review package: {error}",
        )


def canonical_checkpoint_review_package_bytes(package: CheckpointPackage) -> bytes:
    return _canonical_bytes(package) + b"\n"


def decode_canonical_checkpoint_review_package(
    data: bytes,
) -> work_brief_models.WorkBriefResult[CheckpointPackage]:
    package = decode_checkpoint_review_package(data)
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return package
    if data != canonical_checkpoint_review_package_bytes(package):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.PACKAGE_NOT_CANONICAL,
            "Checkpoint review package bytes are not the canonical msgspec encoding.",
        )
    return package


def decode_completion_review_package(
    data: bytes,
) -> work_brief_models.WorkBriefResult[work_brief_models.CompletionReviewPackageValue]:
    try:
        schema_raw = msgspec.json.decode(data, type=dict[str, msgspec.Raw]).get("schema")
        schema = None if schema_raw is None else msgspec.json.decode(schema_raw, type=str)
        if schema == "pinboard-completion-review-package/v1":
            return msgspec.json.decode(data, type=work_brief_models.CompletionReviewPackageV1)
        return msgspec.json.decode(data, type=work_brief_models.CompletionReviewPackage)
    except msgspec.DecodeError as error:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.PACKAGE_INVALID,
            f"Cannot decode completion review package: {error}",
        )


def canonical_completion_review_package_bytes(package: work_brief_models.CompletionReviewPackageValue) -> bytes:
    return _canonical_bytes(package) + b"\n"


def decode_canonical_completion_review_package(
    data: bytes,
) -> work_brief_models.WorkBriefResult[work_brief_models.CompletionReviewPackageValue]:
    package = decode_completion_review_package(data)
    if isinstance(package, work_brief_models.WorkBriefFailure):
        return package
    if data != canonical_completion_review_package_bytes(package):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.PACKAGE_NOT_CANONICAL,
            "Completion review package bytes are not the canonical msgspec encoding.",
        )
    return package


def validate_work_brief_review(
    review: WorkBriefReviewValue,
    brief: work_brief_models.ReadableWorkBrief,
    reviewer_task_id: str | None = None,
) -> work_brief_models.WorkBriefFailure | None:
    checkpoint = _review_checkpoint(brief)
    if isinstance(checkpoint, work_brief_models.WorkBriefFailure):
        return checkpoint
    if review.attempt_id != brief.attempt_id or review.checkpoint_id != checkpoint.checkpoint_id:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_INVALID,
            "Brief review names a different attempt or checkpoint.",
        )
    owner_task_id = brief.owner_task_id if reviewer_task_id is None else reviewer_task_id
    if review.reviewer_task_id == owner_task_id:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_INDEPENDENT,
            "The brief reviewer must be a different task from the attempt owner.",
        )
    if isinstance(brief, (work_brief_models.WorkBrief, work_brief_compatibility_models.WorkBriefV3)):
        if not isinstance(review, work_brief_models.WorkBriefReview):
            return work_brief_models.WorkBriefFailure(
                work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
                "Work brief v3 and later require a ready review bound to the exact accepted brief.",
            )
        accepted_brief_stale = (
            review.accepted_brief_sha256 != hashlib.sha256(canonical_work_brief_bytes(brief)).hexdigest()
        )
    else:
        if not isinstance(review, work_brief_compatibility_models.WorkBriefReviewV2):
            return work_brief_models.WorkBriefFailure(
                work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
                "Retained work brief v2 requires its exact retained ready-review format.",
            )
        accepted_brief_stale = False
    if (
        accepted_brief_stale
        or review.checkpoint_sha256 != hashlib.sha256(canonical_checkpoint_bytes(checkpoint)).hexdigest()
        or (
            review.reviewed_authority_set_sha256
            != hashlib.sha256(canonical_reviewed_authority_set_bytes(checkpoint.reviewed_authorities)).hexdigest()
        )
    ):
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_STALE,
            "Brief review is not bound to the exact accepted brief, checkpoint, and reviewed authorities.",
        )
    expected = {(record.authority_id, record.family, _owner_key(record.owner)) for record in checkpoint.coverage}
    observed = {(record.authority_id, record.family, _owner_key(record.owner)) for record in review.coverage}
    if len(observed) != len(review.coverage) or observed != expected:
        return work_brief_models.WorkBriefFailure(
            work_brief_models.WorkBriefErrorCode.REVIEW_NOT_READY,
            "Brief review must contain exactly one covered result for every coverage owner.",
        )
    return None


def ready_review_key_sha256(brief: work_brief_models.ReadableWorkBrief) -> str:
    """Return the durable ready-review key without reinterpreting retained v2 evidence."""

    if isinstance(brief, (work_brief_models.WorkBrief, work_brief_compatibility_models.WorkBriefV3)):
        return hashlib.sha256(canonical_work_brief_bytes(brief)).hexdigest()
    return hashlib.sha256(canonical_checkpoint_bytes(brief.checkpoint)).hexdigest()


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


def _obligation_target_text(target: work_brief_models.ObligationTarget) -> str:
    match target:
        case work_brief_models.ContractObligationTarget(invariant=invariant):
            return f"contract:{invariant}"
        case work_brief_models.CriterionObligationTarget(number=number):
            return f"criterion:{number}"
        case work_brief_models.DeferralObligationTarget(deferral_id=deferral_id):
            return f"deferral:{deferral_id}"
        case _ as unreachable:
            assert_never(unreachable)


def render_work_brief_markdown(brief: WorkBriefValue) -> bytes:  # noqa: PLR0912 - closed brief projection
    checkpoint = brief.checkpoint
    lines = [
        "---",
        "kind: work-attempt-view",
        f"authority: {brief.schema}",
        f"attempt: {brief.attempt_id}",
        f"item_id: {brief.item_id}",
        f"branch: {brief.branch}",
        f"base_revision: {brief.base_revision}",
        f"owner_task_id: {brief.owner_task_id}",
        f"accepted_scope_revision: {brief.accepted_scope.revision}",
        f"accepted_scope_digest: {brief.accepted_scope.digest}",
        f"artifact_revision: {brief.artifact_revision}",
        *(
            (f"checkout_selection: {brief.checkout_selection.value}",)
            if not isinstance(brief, work_brief_compatibility_models.WorkBriefV2)
            else ()
        ),
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
    if not isinstance(brief, work_brief_compatibility_models.WorkBriefV2):
        lines.extend(("## Obligation correspondence", ""))
        lines.extend(
            f"- `{row.obligation_id}` — `{_obligation_target_text(row.target)}`"
            for row in brief.obligation_correspondence
        )
        lines.append("")
    if isinstance(
        checkpoint,
        (work_brief_models.CrossBoundaryCheckpoint, work_brief_compatibility_models.CrossBoundaryCheckpointV3),
    ):
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
    match brief:
        case work_brief_models.WorkBrief(checkpoint=current_checkpoint):
            match current_checkpoint.disposition:
                case work_brief_models.ContinueCheckpointDisposition(remaining_work=remaining_work):
                    lines.extend(("", "## Checkpoint disposition", "", "Continue", ""))
                    lines.extend(("## Remaining work", "", remaining_work, ""))
                case work_brief_models.TerminalCheckpointDisposition():
                    lines.extend(("", "## Checkpoint disposition", "", "Terminal", ""))
                case _ as unreachable:
                    assert_never(unreachable)
        case (
            work_brief_compatibility_models.WorkBriefV3(remaining_work=remaining_work)
            | work_brief_compatibility_models.WorkBriefV2(remaining_work=remaining_work)
        ):
            lines.extend(("", "## Remaining work", "", remaining_work, ""))
        case _ as unreachable:
            assert_never(unreachable)
    return "\n".join(lines).encode()


def decode_work_brief_identity(data: bytes) -> work_brief_models.WorkBriefResult[WorkBriefIdentity]:
    brief = decode_canonical_work_brief(data)
    if isinstance(brief, work_brief_models.WorkBriefFailure):
        return brief
    return WorkBriefIdentity(
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
    )


def current_attempt_work_brief_identity(
    brief: work_brief_models.WorkBrief,
    artifact_ref_id: ArtifactRefId,
) -> CurrentAttemptWorkBriefIdentity:
    disposition = brief.checkpoint.disposition
    return CurrentAttemptWorkBriefIdentity(
        brief.attempt_id,
        brief.item_id,
        brief.branch,
        brief.base_revision,
        brief.accepted_scope.revision,
        brief.accepted_scope.digest,
        artifact_ref_id,
        "continue" if isinstance(disposition, work_brief_models.ContinueCheckpointDisposition) else "terminal",
    )


def read_selected_work_brief_identity(
    reference: stored_state.ArtifactReference | BriefArtifactRef | None,
    artifacts: ArtifactReader,
) -> DecisionResult[WorkBriefIdentity | None]:
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return None
    identity = decode_work_brief_identity(artifacts.read(reference))
    if isinstance(identity, work_brief_models.WorkBriefFailure):
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
) -> work_brief_models.WorkBriefResult[bytes]:
    if reference is None or reference.kind != work_models.ArtifactKind.BRIEF:
        return _invalid(f"Live attempt '{attempt.attempt_id}' has no accepted brief reference.")
    if not reference.selector.endswith(".json"):
        return _invalid(f"Live attempt '{attempt.attempt_id}' accepted brief is not canonical typed JSON.")
    brief = decode_canonical_work_brief(artifacts.read(reference))
    if isinstance(brief, work_brief_models.WorkBriefFailure):
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
) -> work_brief_models.WorkBriefResult[dict[AttemptId, bytes]]:
    result: dict[AttemptId, bytes] = {}
    references = {value.artifact_ref_id: value for value in state.artifact_references}
    for attempt in state.lifecycle.attempts:
        if attempt.state == work_models.AttemptState.DONE:
            continue
        reference = references.get(attempt.brief_artifact_ref_id)
        rendered = _render_attempt_brief_view(attempt, reference, artifacts)
        if isinstance(rendered, work_brief_models.WorkBriefFailure):
            return rendered
        result[attempt.attempt_id] = rendered
    return result


def build_selected_attempt_brief_views(
    attempts: tuple[query_models.AttemptProjectionFacts, ...], artifacts: ArtifactReader
) -> work_brief_models.WorkBriefResult[dict[AttemptId, bytes]]:
    """Render only the accepted briefs required by selected attempt views."""

    result: dict[AttemptId, bytes] = {}
    for selected in attempts:
        attempt = selected.attempt
        if attempt.state == work_models.AttemptState.DONE:
            continue
        rendered = _render_attempt_brief_view(attempt, selected.brief_reference, artifacts)
        if isinstance(rendered, work_brief_models.WorkBriefFailure):
            return rendered
        result[attempt.attempt_id] = rendered
    return result
